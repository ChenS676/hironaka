import abc
import contextlib
import logging
from copy import deepcopy
from typing import List, Any, Dict, Union, Callable, Optional, Tuple

import torch
import yaml
from torch import nn
from torch.utils.tensorboard import SummaryWriter

from hironaka.core import TensorPoints
from .FusedGame import FusedGame
from .ReplayBuffer import ReplayBuffer
from .Scheduler import ConstantScheduler, ExponentialLRScheduler, ExponentialERScheduler
from .Timer import Timer
from .nets import create_mlp, AgentFeatureExtractor, HostFeatureExtractor
from .player_modules import DummyModule, RandomHostModule, AllCoordHostModule, RandomAgentModule, ChooseFirstAgentModule


class Trainer(abc.ABC):
    """
        Build all the facilities and handle training. Largely inspired by stable-baselines3, but we fuse everything
            together for better clarity and easier modifications.
        To maximize performances and lay the foundation for distributed training, we skip gym environments and game
            wrappers (Host, Agent, Game, etc.).

        A Trainer (and its subclasses) is responsible for training a pair of host/agent networks.
        A few important points before using/inheriting:
          - All parameters come from one single nested dict `config` as the positional argument in the constructor.
            A sample config should be given in YAML format for every implementation.
          - NONE of the keys in config may have default values in the class. Not having a lazy mode means the user
            is educated/reminded about every parameter that goes into the RL training. Also avoids messy parameter
            passing when there are crazy chains of configs from subclasses with clashing keys.
            It is okay to have optional config keys though (default: None).
          - Please include role-specific hyperparameters in `role_specific_hyperparameters`. The rest is taken care of.
            You can find the parameters in the dict returned by `get_all_role_specific_param()`

        Please implement:
            _train()
        Feel free to override:
            _make_network()  # override if one wants to involve more complicated network structures (CNN, GNN, ...).
            _update_learning_rate()
            _generate_rollout()
            copy()  # override if a subclass needs to copy other models/variables.
            save()  # override if a subclass needs to save other models/variables.
            load()

    """
    optim_dict = {'adam': torch.optim.Adam,
                  'sgd': torch.optim.SGD}
    lr_scheduler_dict = {'constant': ConstantScheduler,
                         'exponential': ExponentialLRScheduler}
    er_scheduler_dict = {'constant': ConstantScheduler,
                         'exponential': ExponentialERScheduler}
    replay_buffer_dict = {'base': ReplayBuffer}

    # Please include role-specific hyperparameters that only require simple assignments to attributes.
    #   (except exploration_rate due to more complex nature).
    # Note that all the parameters defined here can be obtained by calling `get_all_role_specific_param()`.
    role_specific_hyperparameters = ['batch_size', 'initial_rollout_size', 'max_rollout_step']

    # Clarify and suppress IDE warnings.
    host_net = None
    agent_net = None

    def __init__(self,
                 config: Union[Dict[str, Any], str],  # Either the config dict or the path to the YAML file
                 node: int = 0,  # For distributed training: the number of node
                 device_num: int = 0,  # For distributed training: the number of cuda device
                 host_net: Optional[nn.Module] = None,  # Pre-assigned host_net. Will ignore host config if set.
                 agent_net: Optional[nn.Module] = None,  # Pre-assigned agent_net. Will ignore agent config if set.
                 reward_func: Optional[Callable] = None
                 ):
        self.logger = logging.getLogger(__class__.__name__)

        self.node = node
        self.device_num = device_num

        if isinstance(config, str):
            self.config = self.load_yaml(config)
        elif isinstance(config, dict):
            self.config = config
        else:
            raise TypeError(f"config must be either a str or dict. Got{type(config)}.")

        # Initialize persistent variables
        self.total_num_steps = 0  # Record the total number of training steps

        # -------- Handle Configurations -------- #

        # Highest-level mandatory configs:
        self.use_tensorboard = self.config['use_tensorboard']
        self.layerwise_logging = self.config['layerwise_logging']
        self.log_time = self.config['log_time']
        self.use_cuda = self.config['use_cuda']
        self.scale_observation = self.config['scale_observation']
        self.version_string = self.config['version_string']

        self.dimension = self.config['dimension']
        self.max_num_points = self.config['max_num_points']
        self.max_value = self.config['max_value']

        # Create time log
        self.time_log = dict()

        # The suffix string used for logging and saving
        self.string_suffix = f"-{self.version_string}-node_{node}-cuda_{device_num}"

        # Initialize TensorBoard settings
        if self.use_tensorboard:
            self.tb_writer = SummaryWriter(comment=self.string_suffix)

        # Set torch device
        self.node = node
        self.device = torch.device(f'cuda:{device_num}') if self.use_cuda else torch.device('cpu')

        # Initialize host and agent parameters
        roles = ['host', 'agent']
        heads = [HostFeatureExtractor, AgentFeatureExtractor]
        output_dims = [2 ** self.dimension, self.dimension]
        pretrained_nets = [host_net, agent_net]

        # Set the reward function
        self.reward_func = reward_func

        for role, head_cls, output_dim, pretrained_net in zip(roles, heads, output_dims, pretrained_nets):
            # Initialize hyperparameters
            for key in self.role_specific_hyperparameters:
                setattr(self, f'{role}_{key}', self.config[role][key])

            net_arch = self.config[role]['net_arch']
            assert isinstance(net_arch, list), f"'net_arch' must be a list. Got {type(net_arch)}."

            optim = self.config[role]['optim']['name']
            permissible_optims = ['adam', 'sgd']
            assert optim in permissible_optims, f"'optim' must be one of {permissible_optims}. Got {optim}."

            # Construct networks
            if pretrained_net is not None:
                setattr(self, f'{role}_net', pretrained_net)
            else:
                head = head_cls(self.dimension, self.max_num_points)
                input_dim = head.feature_dim
                setattr(self, f'{role}_net', self._make_network(head, net_arch, input_dim, output_dim).to(self.device))

            # Ignore the rest of the loop if we are given a dummy net (without trainable parameters)
            if isinstance(pretrained_net, DummyModule):
                setattr(self, f'{role}_is_dummy', True)
                continue
            setattr(self, f'{role}_is_dummy', False)

            # Construct optimizers
            cfg = self.config[role]['optim'].copy()
            setattr(self, f'{role}_optim_config', cfg)
            self._set_optim(role)

            # Construct learning rate scheduler
            lr = cfg['args']['lr']
            if 'lr_schedule' in cfg:
                lr_scheduler = self.lr_scheduler_dict[cfg['lr_schedule']['mode']](lr, **cfg['lr_schedule'])
            else:
                lr_scheduler = None
            setattr(self, f'{role}_lr_scheduler', lr_scheduler)

            # Construct exploration rate scheduler
            cfg = self.config[role]
            er = cfg['er']
            if 'er_schedule' in cfg:
                er_scheduler = self.er_scheduler_dict[cfg['er_schedule']['mode']](er, **cfg['er_schedule'])
            else:
                er_scheduler = ConstantScheduler(er)
            setattr(self, f'{role}_er_scheduler', er_scheduler)

            # Construct replay buffer (same setting for both host and agent)
            cfg = self.config['replay_buffer']
            if role == 'host':
                input_shape = (self.max_num_points, self.dimension)
            elif role == 'agent':
                input_shape = {'points': (self.max_num_points, self.dimension),
                               'coords': (self.dimension,)}
            else:
                raise Exception('Impossible code path.')

            replay_buffer = self.replay_buffer_dict[cfg['type']](
                input_shape=input_shape,
                output_dim=output_dim,
                device=self.device,
                **cfg)
            setattr(self, f'{role}_replay_buffer', replay_buffer)

        # -------- Initialize states -------- #

        # Construct FusedGame
        self._make_fused_game()
        # Generate initial collections of replays
        self.collect_rollout('host', getattr(self, 'host_initial_rollout_size'))
        self.collect_rollout('agent', getattr(self, 'agent_initial_rollout_size'))

    def replace_nets(self, host_net: nn.Module = None, agent_net: nn.Module = None) -> None:
        """
            Override the internal host_net and agent_net with custom networks.
            It is the user's responsibility to make sure the input dimension and the output dimension are correct.
        """
        for role, net in zip(['host', 'agent'], [host_net, agent_net]):
            if net is not None:
                setattr(self, f'{role}_net', net.to(self.device))
                if not isinstance(net, DummyModule):
                    self._set_optim(role)
        self._make_fused_game()

    def replace_reward_func(self, reward_func: Callable):
        """
            Replace the reward function by a new one, and reconstruct the self.fused_game object
        """
        self.reward_func = reward_func
        self._make_fused_game()

    def train(self, steps: int, evaluation_interval: int = 1000, **kwargs):
        """
            Train the networks for a number of steps.
            The definition of 'step' is up to the subclasses. Ideally, each step is one unit that updates both host
                and agent together (but, for example, could already be many epochs of gradient descent.)
        """
        self.set_training(True)
        # The subclass will implement the training logic in _train()
        # Note: `self.total_num_steps` is left for _train() to control.
        with Timer('train_total', self.time_log, active=self.log_time, use_cuda=self.use_cuda):
            self._train(steps, evaluation_interval=evaluation_interval, **kwargs)
        # We always reset training mode to False outside training.
        self.set_training(False)

    def collect_rollout(self, role: str, num_of_games: int):
        """
            Play random games `num_of_games` number of times, and add all the outputs into the replay buffer.
            Parameters:
                role: str. Either 'host' or 'agent'.
                num_of_games: int. Number of steps to play.
        """
        if self.is_dummy(role):
            return

        param = self.get_all_role_specific_param(role)
        replay_buffer = self.get_replay_buffer(role)

        exps = self.get_rollout(role, num_of_games, param['max_rollout_step'])
        for exp in exps:
            replay_buffer.add(*exp, clone=True)

    def get_rollout(self, role: str, num_of_games: int, steps: int, er: float = None) -> List[Any]:
        """
            Generate roll-out `step` number of times, and return the experiences as a tuple
                (obs, act, rew, done, next_obs).
            Parameters:
                role: str. Either 'host' or 'agent'.
                num_of_games: int. Number of games to play.
                steps: int. Number of maximal steps to play.
                er: int. Learning rate.
        """
        er = self.get_er(role) if er is None else er
        points = self._generate_random_points(num_of_games)
        exps = []

        for i in range(steps):
            if not points.ended:
                exps.append(self.fused_game.step(points, role,
                                                 scale_observation=self.scale_observation,
                                                 exploration_rate=er))
        return exps

    def evaluate_rho(self, num_samples: int = 1000, max_steps: int = 100) -> List[torch.Tensor]:
        """
            Estimate the rho value for pairs:
                host_net vs (agent_net, RandomAgent, ChooseFirstAgent)
                (RandomHost, AllCoordHost) vs agent_net
        """
        result = []
        dummy_param = (self.dimension, self.max_num_points, self.device)
        hosts = [self.host_net]*3 + [RandomHostModule(*dummy_param), AllCoordHostModule(*dummy_param)]
        agents = [self.agent_net, RandomAgentModule(*dummy_param), ChooseFirstAgentModule(*dummy_param)] + \
                 [self.agent_net]*2

        for host, agent in zip(hosts, agents):
            points = self._generate_random_points(num_samples)
            fused_game = FusedGame(host, agent, device_key=self.device_key, reward_func=self.reward_func)
            initial = sum(points.ended_batch_in_tensor)
            previous = initial
            total_steps = 0
            for i in range(max_steps):
                host_move, _ = fused_game.host_move(points, exploration_rate=0.)
                fused_game.agent_move(points, host_move,
                                      scale_observation=self.scale_observation,
                                      inplace=True,
                                      exploration_rate=0.)
                new_ended = sum(points.ended_batch_in_tensor) - previous
                total_steps += new_ended * (i + 1)
                previous = sum(points.ended_batch_in_tensor)
            total_steps += sum(~points.ended_batch_in_tensor) * max_steps
            result.append((num_samples - initial) / total_steps)
        return result

    def count_actions(self, role: str, games: int, max_steps: int = 100, er: float = None) -> torch.Tensor:
        rollouts = self.get_rollout(role, games, max_steps, er=er)
        if role == 'host':
            max_num = 2**self.dimension
        elif role == 'agent':
            max_num = self.dimension
        else:
            raise Exception(f'role must be either host or agent. Got {role}.')

        count = torch.bincount(rollouts[0][1].flatten(), minlength=max_num)
        for i in range(1, len(rollouts)):
            count += torch.bincount(rollouts[i][1].flatten(), minlength=max_num)
        return count

    def copy(self):
        """
            Copy the models and the config to create a new object. (Caution: ReplayBuffer is NOT copied).
            If a subclass would like to copy other models or variables, it MUST be overridden.
        """
        return self.__class__(self.config, node=self.node, device_num=self.device_num,
                              host_net=deepcopy(self.host_net), agent_net=deepcopy(self.agent_net))

    def save(self, path: str):
        """
            Save only models and config as a dict (Caution: ReplayBuffer is NOT saved).
            If a subclass creates extra models (e.g., DQNTrainer.{role}_q_net_target), it MUST be overridden.
        """
        saved = {'host_net': self.host_net, 'agent_net': self.agent_net, 'config': self.config}
        torch.save(saved, path)

    def save_replay_buffer(self, path: str):
        """
            Save replay buffers as a dict.
        """
        saved = {'host_replay_buffer': self.get_replay_buffer('host'),
                 'agent_replay_buffer': self.get_replay_buffer('agent')}
        torch.save(saved, path)

    def load_replay_buffer(self, path: str):
        """
            Load replay buffers from file.
        """
        saved = torch.load(path)
        for key in ['host_replay_buffer', 'agent_replay_buffer']:
            if key not in saved or saved[key] is None:
                continue

            if saved[key].actions.shape != getattr(self, key).actions.shape:
                self.logger.warning(
                    f"The shape of the replay buffers might be different! Got {saved[key].actions.shape} \
                    and {getattr(self, key).actions.shape} on action attributes.")
            setattr(self, key, saved[key])

    @classmethod
    def load(cls, path: str, node: int = 0, device_num: int = 0):
        """
            Load from the model-config dict and reconstruct the Trainer object.
        """
        saved = torch.load(path)
        new_trainer = cls(saved['config'], node=node, device_num=device_num)
        new_trainer.replace_nets(saved['host_net'], saved['agent_net'])
        return new_trainer

    @abc.abstractmethod
    def _train(self, steps: int, evaluation_interval: int = 1000, **kwargs):
        pass

    # -------- Role specific getters -------- #

    def get_all_role_specific_param(self, role):
        if not hasattr(self, f'{role}_all_param'):
            result = {}
            for key in self.role_specific_hyperparameters:
                result[key] = getattr(self, f'{role}_{key}')
            setattr(self, f'{role}_all_param', result)
        return getattr(self, f'{role}_all_param')

    def get_net(self, role):
        return getattr(self, f'{role}_net')

    def get_optim(self, role):
        return getattr(self, f'{role}_optimizer', None)

    def get_lr_scheduler(self, role):
        return getattr(self, f'{role}_lr_scheduler', None)

    def get_er_scheduler(self, role):
        return getattr(self, f'{role}_er_scheduler', None)

    def get_er(self, role):
        return self.get_er_scheduler(role)(self.total_num_steps)

    def get_replay_buffer(self, role):
        return getattr(self, f'{role}_replay_buffer', None)

    def get_batch_size(self, role):
        return getattr(self, f'{role}_batch_size')

    def is_dummy(self, role):
        return getattr(self, f'{role}_is_dummy')

    # -------- Set internal parameters (used outside initialization) -------- #

    def set_learning_rate(self):
        for role in ['host', 'agent']:
            optimizer = self.get_optim(role)
            scheduler = self.get_lr_scheduler(role)
            if scheduler is None:
                return
            self._update_learning_rate(optimizer, scheduler(self.total_num_steps))

    def set_training(self, training_mode: bool):
        for role in ['host', 'agent']:
            self.get_net(role).train(training_mode)

    @contextlib.contextmanager
    def inference_mode(self):
        self.set_training(False)
        try:
            yield
        finally:
            self.set_training(True)

    # -------- Private utility methods -------- #

    @staticmethod
    def _update_learning_rate(optimizer, new_lr):
        for param_group in optimizer.param_groups:
            param_group["lr"] = new_lr

    def _make_fused_game(self):
        device_key = self.device_key
        self.fused_game = FusedGame(self.host_net, self.agent_net, device_key=device_key, reward_func=self.reward_func)

    def _set_optim(self, role: str):
        """
            Use `self.{role}_optim_config` to (re-)build `self.{role}_optimizer` using `self.{role}_net.parameters()`
        """
        cfg = getattr(self, f'{role}_optim_config')
        net = getattr(self, f'{role}_net')
        setattr(self, f'{role}_optimizer', self.optim_dict[cfg['name']](net.parameters(), **cfg['args']))

    @staticmethod
    def _make_network(head: nn.Module, net_arch: list, input_dim: int, output_dim: int) -> nn.Module:
        return create_mlp(head, net_arch, input_dim, output_dim)

    def _generate_random_points(self, samples: int) -> TensorPoints:
        pts = torch.randint(self.max_value + 1, (samples, self.max_num_points, self.dimension), dtype=torch.float32,
                            device=self.device)
        points = TensorPoints(pts, device_key=self.device_key)
        points.get_newton_polytope()
        if self.scale_observation:
            points.rescale()
        return points

    # -------- Public static helpers -------- #

    @staticmethod
    def load_yaml(file_path: str) -> dict:
        with open(file_path, "r") as stream:
            config = yaml.safe_load(stream)
        return config

    @property
    def device_key(self):
        return f'cuda:{self.device_num}' if self.use_cuda else 'cpu'
