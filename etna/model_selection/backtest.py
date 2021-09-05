from copy import deepcopy
from enum import Enum
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

import pandas as pd
from joblib import Parallel
from joblib import delayed

from etna.datasets.tsdataset import TSDataset
from etna.metrics import Metric
from etna.metrics import MetricAggregationMode
from etna.models.base import Model
from etna.transforms.base import Transform

TTimeRanges = Tuple[Tuple[Optional[str], str], Tuple[str, str]]


class CrossValidationMode(Enum):
    """Enum for different cross-validation modes."""

    expand = "expand"
    constant = "constant"


class TimeSeriesCrossValidation:
    """Cross validation for time series."""

    def __init__(
        self, model: Model, horizon: int, metrics: List[Metric], n_folds: int = 5, mode: str = "expand", n_jobs: int = 1
    ):
        """
        Init TimeSeriesCrossValidation.

        Parameters
        ----------
        model:
            model to validate
        horizon:
            forecasting horizon
        metrics:
            dict of metric to compute in validation in format {"metric_name": metric};
            note that all the metrics should be in 'per-segment' mode
        n_folds:
            number of timestamp range splits
        mode:
            one of 'expand', 'constant' -- train generation policy
        logger:
            logger where events and metrics are logged
        n_jobs:
            number of jobs to run in parallel

        Raises
        ------
        ValueError:
            if n_folds value is invalid or no metrics given or if some metrics are not in per-segment mode
        NotImplementedError:
            if given mode that is not implemented yet

        Examples
        --------
        If given timeseries is [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], n_folds==3, forecaster.horizon==2 and mode='expandable'
        than generator yields
        [0, 1, 2, 3], [4, 5]
        [0, 1, 2, 3, 4, 5], [6, 7]
        [0, 1, 2, 3, 4, 5, 6, 7], [8, 9]
        In case of 'constant' mode generator yields
        [0, 1, 2, 3], [4, 5]
        [2, 3, 4, 5], [6, 7]
        [4, 5, 6, 7], [8, 9]
        """
        self.model = model
        self.horizon = horizon
        self.n_jobs = n_jobs

        if n_folds < 1:
            raise ValueError(f"Folds number should be a positive number, {n_folds} given")
        self.n_folds = n_folds

        if not metrics:
            raise ValueError("At least one metric required")
        for metric in metrics:
            if not metric.mode == MetricAggregationMode.per_segment:
                raise ValueError(
                    f"All the metrics should be in {MetricAggregationMode.per_segment}, "
                    f"{metric.__class__.__name__} metric is in {metric.mode} mode"
                )
        self.metrics = metrics

        self.mode = CrossValidationMode[mode.lower()]
        if self.mode == CrossValidationMode.expand:
            self._constant_history_length = 0
        elif self.mode == CrossValidationMode.constant:
            self._constant_history_length = 1
        else:
            raise NotImplementedError(
                f"Only '{CrossValidationMode.expand}' and '{CrossValidationMode.constant}' modes allowed"
            )

        self._fold_column = "fold_number"

        self._folds = {}

    def _validate_features(self, ts: TSDataset):
        """
        Check that all the given timestamps have enough timestamp points to validate forecaster with given number of splits.

        Parameters
        ----------
        ts:
            dataset to validate

        Raises
        ------
        ValueError:
            if there is no enough timestamp points to validate forecaster
        """
        # TODO: вернуть старую проверку, когда слайслы в TSDataset не будут возвращать NaNs
        min_required_length = self.horizon * self.n_folds
        segments = set(ts.df.columns.get_level_values("segment"))
        for segment in segments:
            segment_target = ts[:, segment, "target"]
            if any(segment_target.tail(min_required_length).isna()):
                raise ValueError(
                    f"All the series from feature dataframe should contain at least "
                    f"{self.horizon} * {self.n_folds} = {min_required_length} timestamps; "
                    f"series {segment} does not."
                )

    def _generate_folds_dataframes(
        self, ts: TSDataset, transforms: List[Transform] = []
    ) -> Tuple[TSDataset, TSDataset, TSDataset]:
        """
        Generate a sequence of train-test pairs according to timestamp.

        Parameters
        ----------
        ts:
            dataset to split

        Returns
        -------
        tuple of train and test dataset
        """
        timestamps = ts.index
        min_timestamp_idx, max_timestamp_idx = 0, len(timestamps)
        for offset in range(self.n_folds, 0, -1):
            # if not self._constant_history_length, left border of train df is always equal to minimal timestamp value;
            # it means that all the given data is used.
            # if self._constant_history_length, left border of train df moves to one horizon steps on each split
            min_train_idx = min_timestamp_idx + (self.n_folds - offset) * self.horizon * self._constant_history_length
            max_train_idx = max_timestamp_idx - self.horizon * offset - 1
            min_test_idx = max_train_idx + 1
            max_test_idx = max_train_idx + self.horizon

            min_train, max_train = timestamps[min_train_idx], timestamps[max_train_idx]
            min_test, max_test = timestamps[min_test_idx], timestamps[max_test_idx]

            train, test = ts.train_test_split(
                train_start=min_train, train_end=max_train, test_start=min_test, test_end=max_test
            )
            train.fit_transform(transforms=deepcopy(transforms))
            forecast_base = train.make_future(future_steps=self.horizon)
            yield train, test, forecast_base

    def _compute_metrics(self, y_true: TSDataset, y_pred: TSDataset) -> Dict[str, float]:
        """
        Compute metrics for given y_true, y_pred.

        Parameters
        ----------
        y_true:
            dataset of true values of time series
        y_pred:
            dataset of time series forecast
        Returns
        -------
        dict of metrics in format {"metric_name": metric_value}
        """
        metrics = {}
        for metric in self.metrics:
            metrics[metric.__class__.__name__] = metric(y_true=y_true, y_pred=y_pred)
        return metrics

    def get_forecasts(self) -> pd.DataFrame:
        """
        Get forecasts from different folds.

        Returns
        -------
        dataframe with four columns: 'timestamp', 'segment', 'target' and 'fold_number'
        """
        stacked_forecast = pd.DataFrame()
        for fold_number, fold_info in self._folds.items():
            forecast = fold_info["forecast"]
            for segment in forecast.segments:
                forecast.loc[:, pd.IndexSlice[segment, self._fold_column]] = fold_number
            stacked_forecast = stacked_forecast.append(forecast.df)
        return stacked_forecast

    def get_metrics(self, aggregate_metrics: bool = False) -> pd.DataFrame:
        """
        Get dataframe with metrics.

        Parameters
        ----------
        aggregate_metrics:
            if True, returns average metric value over folds for each segment

        Returns
        -------
        dataframe that contains metrics values for different folds if aggregate_metrics=False or
        aggregated metrics otherwise
        """
        metrics_df = pd.DataFrame()

        for i, fold in self._folds.items():
            fold_metrics = pd.DataFrame(fold["metrics"]).reset_index().rename({"index": "segment"}, axis=1)
            fold_metrics[self._fold_column] = i
            metrics_df = metrics_df.append(fold_metrics)

        metrics_df.sort_values(["segment", self._fold_column], inplace=True)

        if aggregate_metrics:
            metrics_df = metrics_df.groupby("segment").mean().reset_index().drop(self._fold_column, axis=1)

        return metrics_df

    def get_fold_info(self) -> pd.DataFrame:
        """
        Get information about folds.

        Returns
        -------
        dataframe with start time and end time for train and test timeranges for different folds
        """
        timerange_df = pd.DataFrame()
        for fold_number, fold_info in self._folds.items():
            tmp_df = pd.DataFrame()
            for stage_name in ("train", "test"):
                for border in ("start", "end"):
                    tmp_df[f"{stage_name}_{border}_time"] = [fold_info[f"{stage_name}_timerange"][border]]
            tmp_df[self._fold_column] = fold_number
            timerange_df = timerange_df.append(tmp_df)
        return timerange_df

    def _run_fold(
        self, train: TSDataset, test: TSDataset, forecast_base: TSDataset, fold_number: int
    ) -> Tuple[int, Dict[int, Any]]:
        """Run fit-forecast pipeline of forecaster for one fold."""
        fold = {}

        for stage_name, stage_df in zip(("train", "test"), (train, test)):
            fold[f"{stage_name}_timerange"] = {}
            fold[f"{stage_name}_timerange"]["start"] = stage_df.index.min()
            fold[f"{stage_name}_timerange"]["end"] = stage_df.index.max()
        model = deepcopy(self.model)
        model.fit(ts=train)
        forecast = model.forecast(ts=forecast_base)
        fold["forecast"] = forecast

        fold["metrics"] = deepcopy(self._compute_metrics(y_true=test, y_pred=forecast))

        return fold_number, fold

    def backtest(
        self, ts: TSDataset, transforms: List[Transform] = ()
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Fit forecasters with historical data and compute metrics for each interval.

        Parameters
        ----------
        ts:
            dataset to fit forecasters with
        transforms:
            list of transforms that should be applied to df in backtest pipeline
        Returns
        -------
            three dataframes: metrics dataframe, forecast dataframe and dataframe with information about folds
        """
        self._validate_features(ts=ts)
        folds = Parallel(n_jobs=self.n_jobs, verbose=11)(
            delayed(self._run_fold)(train=train, test=test, forecast_base=forecast_base, fold_number=i)
            for i, (train, test, forecast_base) in enumerate(
                self._generate_folds_dataframes(ts=ts, transforms=transforms)
            )
        )

        for i, fold in folds:
            self._folds[i] = fold

        metrics_df = self.get_metrics()
        forecast_df = self.get_forecasts()
        fold_info_df = self.get_fold_info()

        return metrics_df, forecast_df, fold_info_df