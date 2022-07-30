import abc
import logging
from typing import List, Any, Dict, Union
import torch
import yaml
from torch import nn
from torch.utils.tensorboard import SummaryWriter

from .nets import create_mlp, AgentFeatureExtractor, HostFeatureExtractor
from .FusedGame import FusedGame
from .ReplayBuffer import ReplayBuffer
from .Scheduler import ConstantScheduler, ExponentialLRScheduler, ExponentialERScheduler
from hironaka.core import TensorPoints
from hironaka.src import merge_experiences


class Trainer(abc.ABC):
    """
        Build all the facilities and handle training. Largely inspired by stable-baseline3, but we fuse everything
            together for better clarity and easier modifications.
        To maximize performances and lay the foundation for distributed training, we skip gym environments and game
            wrappers (Host, Agent, Game, etc.).

        A Trainer (and its subclasses) is responsible for training a pair of host/agent networks.
        A few important points before using/inheriting:
          - All parameters come from a nested dict `config`.
            A sample config is given in YAML format for every implementation.
          - NONE of the keys in config may have default values in the class. Not having a lazy mode means the user
            is educated/reminded about everything that goes into the RL training. Also avoids messy config situations
            like crazy chains of configs from subclasses with clashing keys.
            It is okay to have optional config keys though (default: None).

        Please implement:
            _train()
        Feel free to override:
            _make_network()
            _update_learning_rate()
            _initial_rollout()

    """
    optim_dict = {'adam': torch.optim.Adam,
                  'sgd': torch.optim.SGD}
    lr_scheduler_dict = {'constant': ConstantScheduler,
                         'exponential': ExponentialLRScheduler}
    er_scheduler_dict = {'constant': ConstantScheduler,
                         'exponential': ExponentialERScheduler}
    replay_buffer_dict = {'base': ReplayBuffer}

    # Please include role-specific hyperparameters that only require simple assignments to object attributes.
    #   (except exploration_rate due to more complex nature).
    # If useful, also feel free to add role-specific getter method (unfortunately in a one-by-one fashion).
    role_specific_hyperparameters = ['batch_size', 'initial_rollout_size', 'max_rollout_step']

    # Clarify and suppress IDE warnings.
    host_net = None
    agent_net = None

    def __init__(self,
                 config: Union[Dict[str, Any], str],  # Either the config dict or the path to the YAML file
                 node: int = 0,  # For distributed training: the number of node
                 device_num: int = 0  # For distributed training: the number of cuda device
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

        # Highest-level mandatory configs:
        self.use_tensorboard = self.config['use_tensorboard']
        self.layerwise_logging = self.config['layerwise_logging']
        self.use_cuda = self.config['use_cuda']
        self.scale_observation = self.config['scale_observation']
        self.version_string = self.config['version_string']

        self.dimension = self.config['dimension']
        self.max_num_points = self.config['max_num_points']
        self.max_value = self.config['max_value']

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
        output_dims = [2**self.dimension, self.dimension]

        for role, head_cls, output_dim in zip(roles, heads, output_dims):
            # Initialize hyperparameters
            for key in self.role_specific_hyperparameters:
                setattr(self, f'{role}_{key}', self.config[role][key])

            net_arch = self.config[role]['net_arch']
            assert isinstance(net_arch, list), f"'net_arch' must be a list. Got {type(net_arch)}."

            optim = self.config[role]['optim']['name']
            permissible_optims = ['adam', 'sgd']
            assert optim in permissible_optims, f"'optim' must be one of {permissible_optims}. Got {optim}."

            # Construct networks
            head = head_cls(self.dimension, self.max_num_points)
            input_dim = head.feature_dim
            setattr(self, f'{role}_net', self._make_network(head, net_arch, input_dim, output_dim))
            setattr(self, f'{role}_net_args', (head_cls, net_arch, input_dim, output_dim))

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
            replay_buffer = self.replay_buffer_dict[cfg['type']](
                input_shape=input_shape,
                output_dim=output_dim,
                device=self.device,
                **cfg)
            setattr(self, f'{role}_replay_buffer', replay_buffer)

        # Construct FusedGame
        self._make_fused_game()
        # Generate initial collections of replays
        self._generate_rollout('host', getattr(self, 'host_initial_rollout_size'))
        self._generate_rollout('agent', getattr(self, 'agent_initial_rollout_size'))

        # Initialize persistent variables
        self.total_num_steps = 0  # Record the total number of training steps

    def replace_nets(self, host_net: nn.Module = None, agent_net: nn.Module = None) -> None:
        """
            Override the internal host_net and agent_net with custom networks.
            It is the user's responsibility to make sure the input dimension and the output dimension are correct.
        """
        for role, net in zip(['host', 'agent'], [host_net, agent_net]):
            if net is not None:
                setattr(self, f'{role}_net', net.to(self.device))
                self._set_optim(role)
        self._make_fused_game()

    def train(self, steps: int):
        """
            Train the networks for a number of steps.
            The definition of 'step' is up to the subclasses. Ideally, each step is one unit that updates both host
                and agent together (but, for example, could already be many epochs of gradient descent.)
        """
        self.set_training(True)
        # The subclass will implement the training logic in _train()
        # Note: `self.total_num_steps` is left for _train() to control.
        self._train(steps)
        # We choose to always reset training mode to False outside training.
        self.set_training(False)

    # ------- Role specific getters -------
    def get_all_role_specific_param(self, role):
        if not hasattr(self, f'{role}_all_param'):
            result = {}
            for key in self.role_specific_hyperparameters:
                result[key] = getattr(self, f'{role}_{key}')
            setattr(self, f'{role}_all_param', result)
        return getattr(self, f'{role}_all_param')

    def get_net(self, role):
        return getattr(self, f'{role}_net')

    def get_net_args(self, role):
        return getattr(self, f'{role}_net_args')

    def get_optim(self, role):
        return getattr(self, f'{role}_optimizer')

    def get_lr_scheduler(self, role):
        return getattr(self, f'{role}_lr_scheduler')

    def get_er_scheduler(self, role):
        return getattr(self, f'{role}_er_scheduler')

    def get_replay_buffer(self, role):
        return getattr(self, f'{role}_replay_buffer')

    def get_batch_size(self, role):
        return getattr(self, f'{role}_batch_size')

    # ------- Setting internal parameters -------
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

    @staticmethod
    def _update_learning_rate(optimizer, new_lr):
        for param_group in optimizer.param_groups:
            param_group["lr"] = new_lr

    @abc.abstractmethod
    def _train(self, steps: int):
        pass

    def _generate_rollout(self, role, steps):
        param = self.get_all_role_specific_param(role)

        pts = torch.randint(self.max_value+1, (steps, self.max_num_points, self.dimension), dtype=torch.float)
        points = TensorPoints(pts, device_key=self.device_key)
        points.get_newton_polytope()
        if self.scale_observation:
            points.rescale()

        replay_buffer = self.get_replay_buffer(role)
        replay_buffer.add(*self._roll_out(points, role, 0., param['max_rollout_step']))

    def _roll_out(self, points: TensorPoints, role: str, er: float, steps: int):
        """
            Play games `step` number of times, and combine the output into an experience.
            Parameters:
                points: TensorPoints.
                role: str. Either 'host' or 'agent'. Will have an impact on observation format.
                er: int. Exploration rate.
                steps: int. Number of steps to play.
        """
        roll_outs = [
            self.fused_game.step(points, role, scale_observation=self.scale_observation, exploration_rate=er)
            for _ in range(steps)]
        return merge_experiences(roll_outs)

    def _make_fused_game(self):
        device_key = self.device_key
        self.fused_game = FusedGame(self.host_net, self.agent_net, device_key=device_key)

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

    @staticmethod
    def load_yaml(file_path: str) -> dict:
        with open(file_path, "r") as stream:
            config = yaml.safe_load(stream)
        return config

    @property
    def device_key(self):
        return f'cuda:{self.device_num}' if self.use_cuda else 'cpu'


