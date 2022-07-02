from typing import Any, Dict, Optional

import numpy as np
from gym import spaces

from hironaka.agent import Agent
from hironaka.envs.HironakaBase import HironakaBase


class HironakaAgentEnv(HironakaBase):
    """
        The environment fixes an *Agent* inside, and is expected to receive actions from a host.
    """

    def __init__(self,
                 agent: Agent,
                 config_kwargs: Optional[Dict[str, Any]] = None,
                 **kwargs):
        config_kwargs = dict() if config_kwargs is None else config_kwargs
        super().__init__(**{**config_kwargs, **kwargs})
        self.agent = agent

        self.observation_space = \
            spaces.Box(low=-1.0, high=np.inf, shape=(self.max_number_points, self.dimension), dtype=np.float32)
        self.action_space = spaces.MultiBinary(self.dimension)

    def _post_reset_update(self):
        pass

    def step(self, action: np.ndarray):
        self.agent.move(self._points, [np.where(action == 1)[0]])

        observation = self._get_obs()
        info = self._get_info()
        reward = 1 if self._points.ended else 0

        return observation, reward, self._points.ended, info

    def _get_obs(self):
        return self._get_padded_points()
