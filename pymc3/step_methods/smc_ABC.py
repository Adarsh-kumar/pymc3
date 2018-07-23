"""Sequential Monte Carlo for Approximate Bayesian Computation

Runs on any pymc3 model.

Created on March, 2016

Various significant updates July, August 2016

Made pymc3 compatible November 2016
Renamed to SMC and further improvements March 2017

@author: Hannes Vasyura-Bathke
"""
import numpy as np
import pymc3 as pm
from tqdm import tqdm

import theano
import copy
import warnings

from scipy.linalg import cholesky
from scipy.stats import multivariate_normal
from scipy.stats.mstats import mquantiles

from ..model import modelcontext
from ..vartypes import discrete_types
from ..theanof import inputvars, make_shared_replacements, join_nonshared_inputs
import numpy.random as nr

#from .metropolis import MultivariateNormalProposal
from .metropolis import Proposal

from .arraystep import metrop_select
from ..backends import smc_text as atext

__all__ = ['SMC_ABC', 'sample_smc_abc']

EXPERIMENTAL_WARNING = ("Warning: SMC-ABC is an experimental step method, and not yet "
                        "recommended for use in PyMC3!")


class MultivariateNormalProposal(Proposal):
    def __init__(self, s):
        n, m = s.shape
        if n != m:
            raise ValueError("Covariance matrix is not symmetric.")
        self.n = n
        self.chol = cholesky(s, lower=True)

    def __call__(self, num_draws=None):
        if num_draws is not None:
            b = np.random.randn(self.n, num_draws)
            return np.dot(self.chol, b).T
        else:
            b = np.random.randn(self.n)
            return np.dot(self.chol, b)

    def logp(self, s, value):
        return multivariate_normal(0, cov=s).logpdf(value)

proposal_dists = {'MultivariateNormal': MultivariateNormalProposal}

def choose_proposal(proposal_name, scale=1.):
    """Initialize and select proposal distribution.

    Parameters
    ----------
    proposal_name : string
        Name of the proposal distribution to initialize
    scale : float or :class:`numpy.ndarray`

    Returns
    -------
    class:`pymc3.Proposal` Object
    """
    return proposal_dists[proposal_name](scale)


class SMC_ABC(atext.ArrayStepSharedLLK):
    """Adaptive Transitional Markov-Chain Monte-Carlo sampler class.

    Creates initial samples and framework around the (C)ATMIP parameters

    Parameters
    ----------
    vars : list
        List of variables for sampler
    out_vars : list
        List of output variables for trace recording. If empty unobserved_RVs are taken.
    samples : int
        The number of samples to draw from the last stage, i.e. the posterior. Defaults to 1000.
        The number of samples should be a multiple of `chains`, otherwise the returned number of
        draws will be the lowest closest multiple of `chains`.
    chains : int
        Number of chains per stage has to be a large number of number of n_jobs (processors to be
        used) on the machine.
    n_steps : int
        The number of steps of a Markov Chain. Only works if `tune_interval=0` otherwise it will be
        determined adaptively.
    scaling : float
        Factor applied to the proposal distribution i.e. the step size of the Markov Chain. Only
        works if `tune_interval=0` otherwise it will be determined adaptively.
    covariance : :class:`numpy.ndarray`
        (chains x chains)
        Initial Covariance matrix for proposal distribution, if None - identity matrix taken
    likelihood_name : string
        name of the :class:`pymc3.deterministic` variable that contains the model likelihood.
        Defaults to 'l_like__'
    proposal_name :
        Type of proposal distribution, see smc.proposal_dists.keys() for options
    tune_interval : int
        Number of steps to tune for. If tune=0 no tunning will be used. Default 10. SMC tunes two
        related quantities, the scaling of the proposal distribution (i.e. the step size of Markov
        Chain) and the number of steps of a Markov Chain (i.e. `n_steps`).
    threshold : float
        Determines the change of beta from stage to stage, i.e.indirectly the number of stages,
        the higher the value of threshold the higher the number of stage. Defaults to 0.5. It should
        be between 0 and 1.
    check_bound : boolean
        Check if current sample lies outside of variable definition speeds up computation as the
        forward model wont be executed. Default: True
    model : :class:`pymc3.Model`
        Optional model for sampling step. Defaults to None (taken from context).
    random_seed : int
        Optional to set the random seed.  Necessary for initial population.

    References
    ----------
    .. [Ching2007] Ching, J. and Chen, Y. (2007).
        Transitional Markov Chain Monte Carlo Method for Bayesian Model Updating, Model Class
        Selection, and Model Averaging. J. Eng. Mech., 10.1061/(ASCE)0733-9399(2007)133:7(816),
        816-832. `link <http://ascelibrary.org/doi/abs/10.1061/%28ASCE%290733-9399
        %282007%29133:7%28816%29>`__
    """
    default_blocked = True

    def __init__(self, vars=None, out_vars=None, samples=1000, chains=100, n_steps=25, scaling=1.,
                 covariance=None, likelihood_name='l_like__', proposal_name='MultivariateNormal',
                 tune_interval=10, threshold=0.5, check_bound=True, model=None, random_seed=-1, 
                 epsilons=None, minimum_eps=0.5, iqr_scale=0.5, sum_stats_list=['mean']):

        warnings.warn(EXPERIMENTAL_WARNING)

        if random_seed != -1:
            nr.seed(random_seed)

        model = modelcontext(model)

        if vars is None:
            vars = model.vars

        self.vars = inputvars(vars)

        if out_vars is None:
            if not any(likelihood_name == RV.name for RV in model.unobserved_RVs):
                pm._log.info('Adding model likelihood to RVs!')
                with model:
                    llk = pm.Deterministic(likelihood_name, model.logpt)
            else:
                pm._log.info('Using present model likelihood!')

            out_vars = model.unobserved_RVs
        out_varnames = [out_var.name for out_var in out_vars]

        if covariance is None and proposal_name == 'MultivariateNormal':
            self.covariance = np.eye(sum(v.dsize for v in self.vars))
            scale = self.covariance
        elif covariance is None:
            scale = np.ones(sum(v.dsize for v in self.vars))
        else:
            scale = covariance

        self.proposal_name = proposal_name
        self.proposal_dist = choose_proposal(self.proposal_name, scale=scale)

        self.scaling = np.atleast_1d(scaling)
        self.tune_interval = tune_interval
        self.steps_until_tune = tune_interval

        self.proposal_samples_array = self.proposal_dist(chains)

        self.samples = samples
        self.population = [model.test_point]
        self.n_steps = n_steps
        self.n_steps_final = n_steps
        self.n_steps = n_steps
        self.stage_sample = 0
        self.accepted = 0

        self.all_sum_stats = []
        self.sum_stat_sim = 0 
        self.sum_stats_list = sum_stats_list
        self.minimum_eps = minimum_eps
        self.epsilons = epsilons
        if epsilons is not None:
            self.epsilons.append(minimum_eps)
        self.iqr_scale = iqr_scale

        epsilon = model.observed_RVs[0].distribution.epsilon
        epsilon.set_value(1)

        self.epsilon = epsilon
        self.stage = 0
        self.chain_index = 0
        self.resampling_indexes = np.arange(chains)

        self.threshold = threshold
        self.chains = chains
        self.likelihoods = np.zeros(chains)

        self.likelihood_name = likelihood_name
        self._llk_index = out_varnames.index(likelihood_name)
        self.discrete = np.concatenate([[v.dtype in discrete_types] * (v.dsize or 1) for v in vars])
        self.any_discrete = self.discrete.any()
        self.all_discrete = self.discrete.all()


        shared = make_shared_replacements(self.vars, model)

        self.logp_forw = logp_forw(out_vars, self.vars, shared)        

        super(SMC_ABC, self).__init__(self.vars, out_vars, shared, epsilons)

    def astep(self, q0):
        """[summary]
        
        [description]
        
        Arguments:
            q0 {[type]} -- [description]
        
        Returns:
            [type] -- [description]
        """
        epsilon = self.epsilon

        if self.stage == 0:
            l_new = self.logp_forw(q0)
            l_new[self._llk_index] = np.exp(l_new[self._llk_index])
            q_new = q0
        else:
            for _ in range(5000):
                delta = self.proposal_dist(1)[0,0]
                q_prop = q0 + delta
                l_new = self.logp_forw(q_prop)#[self._llk_index]
                logp_prior = l_new[self._llk_index]

                if np.isfinite(logp_prior):
                    q_new = q_prop
                    s = self.covariance * self.scaling
                    logp_proposal = self.proposal_dist.logp(s, q_new)
                    l_new[self._llk_index] = np.exp(logp_prior - logp_proposal)
                    self.chain_previous_lpoint[self.chain_index] = l_new
                    break
                else:
                    q_new = q0
                    l_new = self.chain_previous_lpoint[self.chain_index]
        return q_new, l_new

    def calc_beta(self):
        """Calculate next tempering beta and importance weights based on current beta and sample
        likelihoods.

        Returns
        -------
        weights : :class:`numpy.ndarray`
            Importance weights (floats)
        epsilon : 
        """
        self.likelihoods = np.ones_like(self.likelihoods)
        weights_un = self.likelihoods
        weights = weights_un / np.sum(weights_un)
        epsilon = _calc_epsilon(self.epsilons, self.population, self.vars, 
                                               self.iqr_scale, self.stage)
        """
        actualizar self.epsilon, como epsilon.set_value(), return epsilon
        
        """
        return weights, epsilon

    def calc_covariance(self):
        """Calculate trace covariance matrix based on importance weights.

        Returns
        -------
        cov : :class:`numpy.ndarray`
            weighted covariances (NumPy > 1.10. required)
        """
        cov = np.cov(self.array_population, aweights=self.weights.ravel(), bias=False, rowvar=0)
        if np.isnan(cov).any() or np.isinf(cov).any():
            raise ValueError('Sample covariances not valid! Likely "chains" is too small!')
        return np.atleast_2d(cov)

    def select_end_points(self, mtrace, chains):
        """Read trace results (variables and model likelihood) and take end points for each chain
        and set as start population for the next stage.

        Parameters
        ----------
        mtrace : :class:`.base.MultiTrace`

        Returns
        -------
        population : list
            of :func:`pymc3.Point` dictionaries
        array_population : :class:`numpy.ndarray`
            Array of trace end-points
        likelihoods : :class:`numpy.ndarray`
            Array of likelihoods of the trace end-points
        """
        array_population = np.zeros((chains, self.ordering.size))
        n_steps = len(mtrace)

        # collect end points of each chain and put into array
        for var, slc, shp, _ in self.ordering.vmap:
            slc_population = mtrace.get_values(varname=var, burn=n_steps - 1, combine=True)
            if len(shp) == 0:
                array_population[:, slc] = np.atleast_2d(slc_population).T
            else:
                array_population[:, slc] = slc_population
        # get likelihoods
        likelihoods = mtrace.get_values(varname=self.likelihood_name,
                                        burn=n_steps - 1, combine=True)

        # map end array_endpoints to dict points
        population = [self.bij.rmap(row) for row in array_population]

        return population, array_population, likelihoods

    def get_chain_previous_lpoint(self, mtrace, chains):
        """Read trace results and take end points for each chain and set as previous chain result
        for comparison of metropolis select.
        Parameters
        ----------
        mtrace : :class:`.base.MultiTrace`
        Returns
        -------
        chain_previous_lpoint : list
            all unobservedRV values, including dataset likelihoods
        """
        array_population = np.zeros((chains, self.lordering.size))
        n_steps = len(mtrace)
        for _, slc, shp, _, var in self.lordering.vmap:
            slc_population = mtrace.get_values(varname=var, burn=n_steps - 1, combine=True)
            if len(shp) == 0:
                array_population[:, slc] = np.atleast_2d(slc_population).T
            else:
                array_population[:, slc] = slc_population

        return [self.lij.rmap(row) for row in array_population[self.resampling_indexes, :]]

    def mean_end_points(self):
        """Calculate mean of the end-points and return point.

        Returns
        -------
        Dictionary of trace variables
        """
        return self.bij.rmap(self.array_population.mean(axis=0))

    def resample(self):
        """Resample pdf based on importance weights. based on Kitagawas deterministic resampling
        algorithm.

        Returns
        -------
        outindex : :class:`numpy.ndarray`
            Array of resampled trace indexes
        """
        parents = np.arange(self.chains)
        N_childs = np.zeros(self.chains, dtype=int)

        cum_dist = np.cumsum(self.weights)
        u = (parents + np.random.rand()) / self.chains
        j = 0
        for i in parents:
            while u[i] > cum_dist[j]:
                j += 1

            N_childs[j] += 1

        indx = 0
        outindx = np.zeros(self.chains, dtype=int)
        for i in parents:
            if N_childs[i] > 0:
                for j in range(indx, (indx + N_childs[i])):
                    outindx[j] = parents[i]

            indx += N_childs[i]

        return outindx


def sample_smc_abc(samples=1000, chains=100, step=None, start=None, homepath=None, stage=0, cores=1,
               tune_interval=10, progressbar=False, model=None, random_seed=-1, rm_flag=True, 
               minimum_eps=None, **kwargs):
    """Sequential Monte Carlo sampling

    Samples the solution space with `chains` of Metropolis chains, where each chain has `n_steps`=`samples`/`chains`
    iterations. Once finished, the sampled traces are evaluated:

    (1) Based on the likelihoods of the final samples, chains are weighted
    (2) the weighted covariance of the ensemble is calculated and set as new proposal distribution
    (3) the variation in the ensemble is calculated and also the next tempering parameter (`beta`)
    (4) New `chains` Markov chains are seeded on the traces with high weight for n_steps iterations
    (5) Repeat until `beta` > 1.

    Parameters
    ----------
    samples : int
        The number of samples to draw from the last stage, i.e. the posterior. Defaults to 1000.
        The number of samples should be a multiple of `chains`, otherwise the returned number of
        draws will be the lowest closest multiple of `chains`.
    chains : int
        Number of chains used to store samples in backend.
    step : :class:`SMC`
        SMC initialization object
    start : List of dictionaries
        with length of (`chains`). Starting points in parameter space (or partial point)
        Defaults to random draws from variables (defaults to empty dict)
    homepath : string
        Result_folder for storing stages, will be created if not existing.
    stage : int
        Stage where to start or continue the calculation. It is possible to continue after completed
        stages (`stage` should be the number of the completed stage + 1). If None the start will be at
        `stage=0`.
    cores : int
        The number of cores to be used in parallel. Be aware that Theano has internal
        parallelization. Sometimes this is more efficient especially for simple models.
        `step.chains / cores` has to be an integer number!
    tune_interval : int
        Number of steps to tune for. Defaults to 10.
    progressbar : bool
        Flag for displaying a progress bar
    model : :class:`pymc3.Model`
        (optional if in `with` context) has to contain deterministic variable name defined under
        `step.likelihood_name` that contains the model likelihood
    random_seed : int or list of ints
        A list is accepted, more if `cores` is greater than one.
    rm_flag : bool
        If True existing stage result folders are being deleted prior to sampling.

    References
    ----------
    .. [Minson2013] Minson, S. E. and Simons, M. and Beck, J. L., (2013),
        Bayesian inversion for finite fault earthquake source models I- Theory and algorithm.
        Geophysical Journal International, 2013, 194(3), pp.1701-1726,
        `link <https://gji.oxfordjournals.org/content/194/3/1701.full>`__
    """
    warnings.warn(EXPERIMENTAL_WARNING)

    model = modelcontext(model)

    if random_seed != -1:
        nr.seed(random_seed)

    """ 
    if step is None:
        pm._log.info('Argument `step` is None. Auto-initialising step object '
                     'using given/default parameters.')
        step = SMC_ABC(chains=chains, tune_interval=tune_interval, model=model, 
                  observed=observed, epsilons=epsilons, minimum_eps=minimum_eps, 
                   iqr_scale=iqr_scale)

    step.minimum_eps = minimum_eps
    step.observed = observed
    step.iqr_scale = iqr_scale
    step.epsilons = epsilons
    """
    if homepath is None:
        raise TypeError('Argument `homepath` should be path to result_directory.')

    if cores > 1:
        if not (step.chains / float(cores)).is_integer():
            raise TypeError('chains / cores has to be a whole number!')

    if start is not None:
        if len(start) != step.chains:
            raise TypeError('Argument `start` should have dicts equal the '
                            'number of chains (`step.chains`)')
        else:
            step.population = start

    if not any(step.likelihood_name in var.name for var in model.deterministics):
        raise TypeError('Model (deterministic) variables need to contain a variable {} as defined '
                        'in `step`.'.format(step.likelihood_name))

    stage_handler = atext.TextStage(homepath)

    if progressbar and cores > 1:
        progressbar = False

    if stage == 0:
        # continue or start initial stage
        step.stage = stage
        draws = 1
    else:
        step = stage_handler.load_atmip_params(stage, model=model)
        draws = step.n_steps

    stage_handler.clean_directory(stage, None, rm_flag)

    x_chains = stage_handler.recover_existing_results(stage, draws, chains, step)

    if start is not None:
        if len(start) != chains:
            raise TypeError('Argument `start` should have dicts equal the '
                            'number of chains (`chains`)')
        else:
            step.population = start
    else:
        step.population = _initial_population(samples, chains, model, step.vars)
    step.epsilon = _calc_epsilon(step.epsilons, step.population, step.vars, step.iqr_scale, step.stage)
    
    pm._log.info('Using {} as summary statistic...'.format(step.sum_stats_list))

    with model:
        while step.epsilon > step.minimum_eps:
        #for step.epsilon in step.epsilons:
            if step.stage == 0:
                # Initial stage
                pm._log.info('Sample initial stage: ...')
                draws = 1
            else:
                draws = step.n_steps

            pm._log.info('Epsilon: {:.4f} Stage: {}'.format(step.epsilon, step.stage))

            # Metropolis sampling intermediate stages
            x_chains = stage_handler.clean_directory(step.stage, chains, rm_flag)
            sample_args = {'draws': draws,
                           'step': step,
                           'stage_path': stage_handler.stage_path(step.stage),
                           'progressbar': progressbar,
                           'model': model,
                           'n_jobs': cores,
                           'x_chains': x_chains,
                           'chains': chains}

            _iter_parallel_chains(**sample_args)

            mtrace = stage_handler.load_multitrace(step.stage)

            step.population, step.array_population, step.likelihoods = step.select_end_points(
                mtrace, chains)

            step.weights, step.epsilon = step.calc_beta()

            if step.epsilon < step.minimum_eps:
                pm._log.info('Epsilon {:.4f} < minimum epsilon {:.4f}'.format(step.epsilon, step.minimum_eps))
                stage_handler.dump_atmip_params(step)
                if stage == -1:
                    x_chains = []
                else:
                    x_chains = None
            else:
                step.covariance = step.calc_covariance()
                step.proposal_dist = choose_proposal(step.proposal_name, scale=step.covariance)
                step.resampling_indexes = step.resample()
                step.chain_previous_lpoint = step.get_chain_previous_lpoint(mtrace, chains)
                stage_handler.dump_atmip_params(step)

                step.stage += 1

        # Metropolis sampling final stage
        pm._log.info('Sample final stage')
        step.stage = -1
        x_chains = stage_handler.clean_directory(step.stage, chains, rm_flag)
        weights_un = step.likelihoods
        step.weights = weights_un / np.sum(weights_un)
        step.covariance = step.calc_covariance()
        step.proposal_dist = choose_proposal(step.proposal_name, scale=step.covariance)
        step.resampling_indexes = step.resample()
        step.chain_previous_lpoint = step.get_chain_previous_lpoint(mtrace, chains)


        x_chains = nr.randint(0, chains, size=samples)

        sample_args['draws'] = step.n_steps_final
        sample_args['step'] = step
        sample_args['stage_path'] = stage_handler.stage_path(step.stage)
        sample_args['x_chains'] = x_chains
        _iter_parallel_chains(**sample_args)

        stage_handler.dump_atmip_params(step)

        return stage_handler.create_result_trace(step.stage,
                                                 step=step,
                                                 model=model)


def _initial_population(samples, chains, model, variables):
    """
    Create an initial population from the prior
    """
    population = []
    init_rnd = {}
    start = model.test_point
    for v in variables:
        if pm.util.is_transformed_name(v.name):
            trans = v.distribution.transform_used.forward_val
            init_rnd[v.name] = trans(v.distribution.dist.random(size=chains, point=start))
        else:
            init_rnd[v.name] = v.random(size=chains, point=start)

    for i in range(chains):
        population.append(pm.Point({v.name: init_rnd[v.name][i] for v in variables}, model=model))

    return population

def _calc_epsilon(epsilons, population, vars, iqr_scale, stage):
    """[summary]
    
    [description]
    
    Arguments:
        epsilons {[type]} -- [description]
        population {[type]} -- [description]
        vars {[type]} -- [description]
        iqr_scale {[type]} -- [description]
        stage {[type]} -- [description]
    
    Returns:
        [type] -- [description]
    """

    if epsilons is None:
        range_iq = mquantiles([d[str(v)] for d in population for v in vars], 
                                  prob=[0.25, 0.75])
        epsilon = np.abs(range_iq[1] - range_iq[0]) * iqr_scale
    else:
        epsilon = epsilons[stage]

    return epsilon

def sum_stats(data, sum_stat=None):
    """
    Parameters:
    -----------
    data : array
        Observed or simulated data
    sum_stat : list
        List of summary statistics to be computed. Accepted strings are mean, std, var. 
        Python functions can be passed in this argument.
        
    Returns:
    --------
    sum_stat_vector : array
        Array contaning the summary statistics.
    """
    
    sum_stat_vector = np.zeros((len(sum_stat), data.shape[1]))
    
    for i, stat in enumerate(sum_stat):
        if stat == 'mean':
            sum_stat_vector[i, 0] =  data.mean()
        elif stat == 'std':
            sum_stat_vector[i, 0] =  data.std()
        elif stat == 'var':
            sum_stat_vector[i, 0] =  data.var()
        else:
            sum_stat_vector[i, 0] =  stat(data)
            
    return sum_stat_vector


def _sample(draws, step=None, start=None, trace=None, chain=0, progressbar=True, model=None,
            random_seed=-1, chain_idx=0):

    sampling = _iter_sample(draws, step, start, trace, chain, model, random_seed, chain_idx)

    if progressbar:
        sampling = tqdm(sampling, total=draws)

    try:
        for strace in sampling:
            pass

    except KeyboardInterrupt:
        pass

    return chain


def _iter_sample(draws, step, start=None, trace=None, chain=0, model=None, random_seed=-1, chain_idx=0):
    """
    Modified from :func:`pymc3.sampling._iter_sample` to be more efficient with SMC algorithm.
    """
    model = modelcontext(model)
    draws = int(draws)
    if draws < 1:
        raise ValueError('Argument `draws` should be above 0.')

    if start is None:
        start = {}

    if random_seed != -1:
        nr.seed(random_seed)

    try:
        step = pm.step_methods.CompoundStep(step)
    except TypeError:
        pass

    point = pm.Point(start, model=model)
    step.chain_index = chain
    trace.setup(draws, chain_idx)
    for i in range(draws):
        point, out_list = step.step(point)
        trace.record(out_list)
        yield trace


def _work_chain(work):
    """Wrapper function for parallel execution of _sample i.e. the Markov Chains.

    Parameters
    ----------
    work : List
        Containing all the information that is unique for each Markov Chain
        i.e. [:class:'SMC', chain_number(int), sampling index(int), start_point(dictionary)]

    Returns
    -------
    chain : int
        Index of chain that has been sampled
    """
    return _sample(*work)


def _iter_parallel_chains(draws, step, stage_path, progressbar, model, n_jobs, chains,
                          x_chains=None):
    """Do Metropolis sampling over all the x_chains with each chain being sampled 'draws' times.
    Parallel execution according to n_jobs.
    """
    if x_chains is None:
        x_chains = range(chains)

    chain_idx = range(0, len(x_chains))
    pm._log.info('Initializing chain traces ...')

    max_int = np.iinfo(np.int32).max

    random_seeds = nr.randint(1, max_int, size=len(x_chains))
    pm._log.info('Sampling ...')

    work = [(draws,
             step,
             step.population[step.resampling_indexes[chain]],
             atext.TextChain(stage_path, model=model),
             chain,
             False,
             model,
             rseed,
             chain_idx) for chain, rseed, chain_idx in zip(x_chains, random_seeds, chain_idx)]

    if draws < 10:
        chunksize = n_jobs
    else:
        chunksize = 1

    p = atext.paripool(_work_chain, work, chunksize=chunksize, nprocs=n_jobs)

    if n_jobs == 1 and progressbar:
        p = tqdm(p, total=len(x_chains))

    for _ in p:
        pass

def tune(acc_rate):
    """Tune adaptively based on the acceptance rate.

    Parameters
    ----------
    acc_rate: float
        Acceptance rate of the Metropolis sampling

    Returns
    -------
    scaling: float
    """
    # a and b after Muto & Beck 2008 .
    a = 1. / 9
    b = 8. / 9
    return (a + b * acc_rate) ** 2


def logp_forw(out_vars, vars, shared):
    """Compile Theano function of the model and the input and output variables.

    Parameters
    ----------
    out_vars : List
        containing :class:`pymc3.Distribution` for the output variables
    vars : List
        containing :class:`pymc3.Distribution` for the input variables
    shared : List
        containing :class:`theano.tensor.Tensor` for depended shared data
    """
    out_list, inarray0 = join_nonshared_inputs(out_vars, vars, shared)
    f = theano.function([inarray0], out_list)
    f.trust_input = True
    return f
