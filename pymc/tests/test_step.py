from .checks import *
from .models import simple_model, mv_simple
from theano.tensor import constant
from scipy.stats.mstats import moment


def check_stat(name, trace, var, stat, value, bound):
    s = stat(trace[var], axis=0)
    close_to(s, value, bound)


def test_step_continuous():
    start, model, (mu, C) = mv_simple()

    hmc = pm.HamiltonianMC(model.vars, C, is_cov=True, model=model)
    mh = pm.Metropolis(model.vars, model=model)
    slicer = pm.Slice(model.vars, model=model)
    compound = pm.CompoundStep([hmc, mh])

    steps = [mh, hmc, compound, slicer]

    unc = np.diag(C) ** .5
    check = [('x', np.mean, mu, unc / 10.),
             ('x', np.std, unc, unc / 10.)]

    for st in steps:
        np.random.seed(1)
        h = sample(8000, st, start, model=model)
        for (var, stat, val, bound) in check:
            np.random.seed(1)
            h = sample(8000, st, start, model=model)

            yield check_stat, repr(st), h, var, stat, val, bound