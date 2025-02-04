from typing import List, Any, Dict, Optional, Union

import numpy as np
import torch

from hironaka.src import get_batched_padded_array, rescale_torch
from hironaka.src import shift_torch, get_newton_polytope_torch, reposition_torch
from .PointsBase import PointsBase


class TensorPoints(PointsBase):
    subcls_config_keys = ['value_threshold', 'device_key', 'padding_value']
    copied_attributes = ['distinguished_points']

    def __init__(self,
                 points: Union[torch.Tensor, List[List[List[float]]], np.ndarray],
                 value_threshold: Optional[float] = 1e8,
                 device_key: Optional[str] = 'cpu',
                 padding_value: Optional[float] = -1.0,
                 distinguished_points: Optional[List[int]] = None,
                 config_kwargs: Optional[Dict[str, Any]] = None,
                 **kwargs):
        config = kwargs if config_kwargs is None else {**config_kwargs, **kwargs}
        self.value_threshold = value_threshold

        assert padding_value <= 0, f"'padding_value' must be a non-positive number. Got {padding_value} instead."

        if isinstance(points, list):
            points = torch.tensor(
                get_batched_padded_array(points,
                                         new_length=config['max_num_points'],
                                         constant_value=padding_value))
        elif isinstance(points, np.ndarray):
            points = torch.tensor(points)
        elif isinstance(points, torch.Tensor):
            points = points.type(torch.float32)
        else:
            raise Exception(f"Input must be a Tensor, a numpy array or a nested list. Got {type(points)}.")

        self.batch_size, self.max_num_points, self.dimension = points.shape

        self.device_key = device_key
        self.padding_value = padding_value
        self.distinguished_points = distinguished_points

        super().__init__(points, **config)
        self.device = torch.device(self.device_key)
        self.points = self.points.to(self.device)

    def exceed_threshold(self) -> bool:
        """
            Check whether the maximal value exceeds the threshold.
        """
        if self.value_threshold is not None:
            return torch.max(self.points) >= self.value_threshold
        return False

    def get_num_points(self) -> torch.Tensor:
        """
            The number of points for each batch.
        """
        num_points = torch.sum(self.points[:, :, 0].ge(0), dim=1)
        return num_points

    def get_features(self):
        sorted_args = torch.argsort(self.points[:, :, 0], dim=1, descending=True)
        return self.points.gather(1, sorted_args.unsqueeze(-1).repeat(1, 1, self.dimension)).clone()

    def _shift(self,
               points: torch.Tensor,
               coords: Union[torch.Tensor, List[List[int]]],
               axis: Union[torch.Tensor, List[int]],
               inplace: Optional[bool] = True,
               ignore_ended_games: Optional[bool] = True,
               **kwargs):
        return shift_torch(points, coords, axis,
                           inplace=inplace,
                           padding_value=self.padding_value,
                           ignore_ended_games=ignore_ended_games)

    def _get_newton_polytope(self, points: torch.Tensor, inplace: Optional[bool] = True, **kwargs):
        return get_newton_polytope_torch(points, inplace=inplace, padding_value=self.padding_value)

    def _get_shape(self, points: torch.Tensor):
        return points.shape

    def _reposition(self, points: torch.Tensor, inplace: Optional[bool] = True, **kwargs):
        return reposition_torch(points, inplace=inplace, padding_value=self.padding_value)

    def _rescale(self, points: torch.Tensor, inplace: Optional[bool] = True, **kwargs):
        return rescale_torch(points, inplace=inplace, padding_value=self.padding_value)

    def _points_copy(self, points: torch.Tensor):
        return points.clone().detach()

    def _add_batch_axis(self, points: torch.Tensor):
        return points.unsqueeze(0)

    def _get_batch_ended(self, points: torch.Tensor):
        num_points = torch.sum(points[:, :, 0].ge(0), 1)
        return num_points.le(1).cpu().detach().tolist()

    @property
    def ended_batch_in_tensor(self):
        return torch.sum(self.points[:, :, 0].ge(0), 1).le(1)

    def __repr__(self):
        return str(self.points)

    def __hash__(self):
        return hash(self.points.detach().cpu().numpy().round(8).tostring())
