from typing import Any, List, Optional, Union

import torch
from torch import Tensor

from sbi.inference.posteriors.base_posterior import NeuralPosterior
from sbi.inference.potentials.base_potential import BasePotential
from sbi.types import Shape, TorchTransform
from sbi.utils import gradient_ascent, mcmc_transform
from sbi.utils.torchutils import ensure_theta_batched
from sbi.utils.user_input_checks import process_x


class NeuralPosteriorEnsemble(NeuralPosterior):
    r"""Wrapper for bundling together different posterior instances into an ensemble.

    This class creates a posterior ensemble from a set of N different, already trained
    posterior estimators $p_{i}(\theta|x_o)$, where $i \in \{i,...,N\}$.

    It can wrap all posterior classes available in sbi and even a mixture of different
    posteriors, i.e. obtained via SNLE and SNPE at the same time, since it only
    provides a pass-through to the class-methods of each posterior in the ensemble. The
    only constraint is, that the individual posteriors have the same prior.

    So far `log_prob()`, `sample()` and `map()` functionality are supported.

    Example:

    ```
    import torch
    from joblib import Parallel, delayed
    from sbi.examples.minimal import simple

    ensemble_size = 10
    posteriors = Parallel(n_jobs=-1)(delayed(simple)() for i in range(ensemble_size))

    ensemble = NeuralPosteriorEnsemble(posteriors)
    ensemble.set_default_x(torch.zeros((3,)))
    ensemble.sample((1,))
    ```

    Attributes:
        posteriors: List of the posterior estimators making up the ensemble.
        num_components: Number of posterior estimators.
        weights: Weight of each posterior distribution. If none are provided each
            posterior is weighted with 1/N.
        potential_fn: Potential function of the ensemble.
        prior: Prior distribution that is the same for all posteriors. If it is not the
            same, then `prior = None`.
        device: Device which the component distributions sit on.
        default_x: Used in `.sample(), .log_prob()` as default conditioning context.
    """

    def __init__(
        self,
        posteriors: List,
        weights: Optional[Union[List[float], Tensor]] = None,
        theta_transform: Optional[TorchTransform] = None,
    ):
        """
        Args:
            posteriors: List containing the trained posterior instances that will make
                up the ensemble.
            weights: Assign weights to posteriors manually, otherwise they will be
                weighted with 1/N.
            theta_transform: If passed, this transformation will be applied during the
            optimization performed when obtaining the map. It does not affect the .
            sample() and .log_prob() methods.
        """
        self.posteriors = posteriors
        self.num_components = len(posteriors)
        self.weights = weights
        self.potential_fn = None  # will be set when context is provided.

        self.device = self.ensure_same_device(posteriors)

        self.prior = self.common_prior()
        if theta_transform is not None:
            self.theta_transform = theta_transform
        elif self.prior is not None:
            self.theta_transform = mcmc_transform(self.prior, device=self.device)
        else:
            self.theta_transform = None

    def common_prior(self) -> Optional[bool]:
        """Sets ensemble prior if all ensemble posteriors have the same prior.

        Args:
            posteriors: List containing the trained posterior instances that will make
                up the ensemble.

        Raises:
            AssertionError if ensemble components have different priors.

        Returns:
            A prior distribution, if it is the same for all posteriors.
        """
        try:
            # for SNPE
            priors = [posterior.prior for posterior in self.posteriors]
        except AttributeError:
            # for SNLE and SNRE
            priors = [posterior.proposal for posterior in self.posteriors]

        # assert same type
        same_type = all(isinstance(prior, type(priors[0])) for prior in priors)

        def compare_params(dist_x, dist_y):
            all_equal = True
            for x, y in zip(dist_x.__dict__.values(), dist_y.__dict__.values()):
                if type(x) == Tensor:
                    all_equal = all_equal and torch.equal(x, y)
                else:
                    all_equal = all_equal and (x == y)
            return all_equal

        if same_type:
            # assert same parameters
            same_params = all(compare_params(prior, priors[0]) for prior in priors)

        same_prior = same_type and same_params
        if same_prior:
            return priors[0]
        else:
            return None

    def ensure_same_device(self, posteriors: List) -> str:
        """Ensures that all posteriors in the ensemble are on the same device.

        Args:
            posteriors: List containing the trained posterior instances that will make
                up the ensemble.

        Raises:
            AssertionError if ensemble components have different device variables.

        Returns:
            A device string, that is the same for all posteriors.
        """
        devices = [posterior._device for posterior in posteriors]
        assert all(
            device == devices[0] for device in devices
        ), "Only supported if all posteriors are on the same device."
        return devices[0]

    @property
    def weights(self) -> Tensor:
        return self._weights

    @weights.setter
    def weights(self, weights: Optional[Union[List[float], Tensor]]) -> Tensor:
        """Set relative weight for each posterior in the ensemble.

        Weights are normalised.

        Raises:
            TypeError if weights are provided in an unsupported format.

        Args:
            weights: Assignes weight to each posterior distribution.
        """
        if weights is None:
            self._weights = torch.tensor(
                [1.0 / self.num_components for _ in range(self.num_components)]
            )
        elif type(weights) == Tensor or type(weights) == List:
            self._weights = torch.tensor(weights) / sum(weights)
        else:
            raise TypeError

    def sample(
        self, sample_shape: Shape = torch.Size(), x: Optional[Tensor] = None, **kwargs
    ) -> Tensor:
        r"""Return samples from posterior ensemble.

        The samples are drawn according to their assigned weight. The number of samples
        for each distributino is drawn from a corresponding multinomial distribution.
        Then each component posterior is sampled individually and all samples are
        aggregated afterwards.

        All kwargs are passed directly through to `posterior.sample()`.

        Args:
            sample_shape: Desired shape of samples that are drawn from posterior
                ensemble. If sample_shape is multidimensional we simply draw
                `sample_shape.numel()` samples and then reshape into the desired shape.
            x: Conditioning context. If none is provided and no default context is set,
                an error will be raised.

        Returns:
            Samples drawn from the ensemble distribution.
        """
        num_samples = torch.Size(sample_shape).numel()
        posterior_indizes = torch.multinomial(
            self._weights, num_samples, replacement=True
        )
        samples = []
        for posterior_index, sample_size in torch.vstack(
            posterior_indizes.unique(return_counts=True)
        ).T:
            sample_shape_c = torch.Size((sample_size,))
            samples.append(
                self.posteriors[posterior_index].sample(sample_shape_c, x=x, **kwargs)
            )
        return torch.vstack(samples).reshape(*sample_shape, -1)

    def log_prob(
        self,
        theta: Tensor,
        x: Optional[Tensor] = None,
        individually: bool = False,
        **kwargs,
    ) -> Tensor:
        r"""Returns the average log-probability of the posterior ensemble

        $\sum_{i}^{N} w_{i} p_i(\theta|x)$.

        All kwargs are passed directly through to `posterior.log_prob()`.

        Args:
            theta: Parameters $\theta$.
            x: Conditioning context.If none is provided and no default context is set,
                an error will be raised.
            individually: If true, returns log weights and log_probs individually.

        Raises:
            AssertionError if posterior estimators are a mixture of different methods.

        Returns:
            `(len(θ),)`-shaped average log posterior probability $\log p(\theta|x)$ for
            θ in the support of the prior, -∞ (corresponding to 0 probability) outside.
        """
        assert all(
            isinstance(posterior, type(self.posteriors[0]))
            for posterior in self.posteriors
        ), "`log_prob()` only works for ensembles of the same type of posterior."

        log_probs = torch.stack(
            [posterior.log_prob(theta, x=x, **kwargs) for posterior in self.posteriors]
        )
        log_weights = torch.log(self._weights).reshape(-1, 1)

        if individually:
            return log_weights, log_probs
        else:
            return torch.logsumexp(log_weights.expand_as(log_probs) + log_probs, dim=0)

    def set_default_x(self, x: Tensor) -> "NeuralPosteriorEnsemble":
        r"""Set new default x for `.sample(), .log_prob()` to use as conditioning context.

        This is a pure convenience to avoid having to repeatedly specify `x` in calls to
        `.sample()` and `.log_prob()` - only θ needs to be passed.

        This convenience is particularly useful when the posterior ensemble is focused,
        i.e. has been trained over multiple rounds to be accurate in the vicinity of a
        particular `x=x_o` (you can check if your posterior object is focused by
        printing one exemplary component of the ensemble).

        NOTE: this method is chainable, i.e. will return the NeuralPosteriorEnsemble
        object so that calls like `posterior_enemble.set_default_x(my_x).sample(mytheta)
        ` are possible.

        Args:
            x: The default observation to set for every posterior $p_i(theta|x)$ in the
            ensemble.
        Returns:
            `NeuralPosteriorEnsemble` that will use a default `x` when not explicitly
            passed.
        """
        for posterior in self.posteriors:
            posterior.set_default_x(x)
        return self

    def potential(
        self, theta: Tensor, x: Optional[Tensor] = None, track_gradients: bool = False
    ) -> Tensor:
        r"""Evaluates $\theta$ under the potential that is used to sample the posterior.
        The potential is the unnormalized log-probability of $\theta$ under the
        posterior.
        Args:
            theta: Parameters $\theta$.
            track_gradients: Whether the returned tensor supports tracking gradients.
                This can be helpful for e.g. sensitivity analysis, but increases memory
                consumption.
        """
        self.potential_fn = EnsemblePotential(
            self.posteriors, self._weights, self.prior, x
        )
        theta = ensure_theta_batched(torch.as_tensor(theta))

        return self.potential_fn(
            theta.to(self._device), track_gradients=track_gradients
        )

    def map(
        self,
        x: Optional[Tensor] = None,
        num_iter: int = 1_000,
        num_to_optimize: int = 100,
        learning_rate: float = 0.01,
        init_method: Union[str, Tensor] = "posterior",
        num_init_samples: int = 1_000,
        save_best_every: int = 10,
        show_progress_bars: bool = False,
        individually: bool = False,
    ) -> Tensor:
        r"""Returns the average maximum-a-posteriori estimate (MAP).

        Computes MAP estimate across the whole ensemble or for each component
        individually. All args and kwargs are passed directly through to
        `gradient_ascent`.

        The routine can be interrupted (individually) with [Ctrl-C], when the user sees
        that the log-probability converges. The best estimate will be saved in `self.
        posteriors[idx].map_`.

        For more details of how the MAP estimate is obtained see `.map()` docstring of
        self.posteriors[idx].

        Args:
            x: Observed data at which to evaluate the MAP.
            num_iter: Number of optimization steps that the algorithm takes
                to find the MAP.
            num_to_optimize: From the drawn `num_init_samples`, use the
                `num_to_optimize` with highest log-probability as the initial points
            learning_rate: Learning rate of the optimizer.
            init_method: How to select the starting parameters for the optimization. If
                it is a string, it can be either [`posterior`, `prior`], which samples
                the respective distribution `num_init_samples` times. If it is a
                tensor, the tensor will be used as init locations.
            num_init_samples: Draw this number of samples from the posterior and
                evaluate the log-probability of all of them.
                for the optimization.
            save_best_every: The best log-probability is computed, saved in the
                `map`-attribute, and printed every `save_best_every`-th iteration.
                Computing the best log-probability creates a significant overhead
                (thus, the default is `10`.)
            show_progress_bars: Whether or not to show a progressbar for sampling from
                the posterior.
            individually: If true, returns log weights and MAPs individually.

        Returns:
            The ensemble MAP estimate or individual log_weigths and component MAP
            estimate if individually == True.
        """

        if individually:
            maps = []
            log_weights = torch.log(self._weights).reshape(-1, 1)

            for posterior in self.posteriors:
                maps.append(
                    posterior.map(
                        x=x,
                        num_iter=num_iter,
                        num_to_optimize=num_to_optimize,
                        learning_rate=learning_rate,
                        init_method=init_method,
                        num_init_samples=num_init_samples,
                        save_best_every=save_best_every,
                        show_progress_bars=show_progress_bars,
                    )
                )
            maps = torch.stack(maps)
            return log_weights, maps

        else:
            if init_method == "posterior":
                inits = self.sample((num_init_samples,))
            elif init_method == "proposal":
                inits = self.proposal.sample((num_init_samples,))
            elif isinstance(init_method, Tensor):
                inits = init_method
            else:
                raise ValueError

            return gradient_ascent(
                potential_fn=self.potential_fn,
                inits=inits,
                theta_transform=self.theta_transform,
                num_iter=num_iter,
                num_to_optimize=num_to_optimize,
                learning_rate=learning_rate,
                save_best_every=save_best_every,
                show_progress_bars=show_progress_bars,
            )[0]


class EnsemblePotential(BasePotential):
    r"""Provides `NeuralPosteriorEnsemble` with a `potential_fn` attribute.

    The potential is the same as the sum of the weighted log-probabilities of each
    component posterior.

    This class was modelled off of `PosteriorBasedPotential` and should provide similar
    functionality.

    Attributes:
        posteriors: List of the posterior estimators making up the ensemble.
        weights: Weight of each posterior distribution. If none are provided each
            posterior is weighted with 1/N.
        prior: Prior distribution that is the same for all posteriors. If it is not the
            same, then `prior = None`.
        device: Device which the component distributions sit on.
        x_o: Used as conditioning context for `potential_fn`.
    """
    allow_iid_x = False  # type: ignore

    def __init__(
        self,
        posteriors: List,
        weights: Tensor,
        prior: Any,
        x_o: Optional[Tensor],
        device: str = "cpu",
    ):
        r"""
        Args:
            posteriors: List containing the trained posterior instances that will make
                up the ensemble.
            weights: Weights of the ensemble components.
            prior: Prior distribution that is the same for all posteriors. If it is not
                the same, then `prior = None`.
            x_o: The observed data at which to evaluate the posterior.
            device: Device which the component distributions sit on.
        """
        self._weights = weights
        self.potential_fns = []
        for posterior in posteriors:
            potential_fn = posterior.potential_fn
            self.potential_fns.append(potential_fn)

        super().__init__(prior, x_o, device)

        self.prior = prior
        self.device = device
        self.set_x(x_o)

    def set_x(self, x_o: Optional[Tensor]):
        """Check the shape of the observed data and, if valid, set it."""
        if x_o is not None:
            x_o = process_x(x_o, allow_iid_x=False).to(self.device)
        self._x_o = x_o
        for comp_potential in self.potential_fns:
            comp_potential.set_x(x_o)

    def __call__(self, theta: Tensor, track_gradients: bool = True) -> Tensor:
        r"""Returns the potential for posterior-based methods.

        Args:
            theta: The parameter set at which to evaluate the potential function.
            track_gradients: Whether to track the gradients.

        Returns:
            The potential.
        """
        theta = ensure_theta_batched(torch.as_tensor(theta))
        theta = theta.to(self.device)

        log_probs = [
            fn(theta, track_gradients=track_gradients) for fn in self.potential_fns
        ]
        log_probs = torch.vstack(log_probs)
        ensemble_log_probs = torch.logsumexp(
            torch.log(self._weights.reshape(-1, 1)).expand_as(log_probs) + log_probs,
            dim=0,
        )
        return ensemble_log_probs
