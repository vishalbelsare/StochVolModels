import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from enum import Enum
from typing import Dict, Tuple, Optional
from numba.typed import List
import matplotlib

import stochvolmodels.pricers.analytic.bachelier as bachel
from stochvolmodels import ExpansionOrder
from stochvolmodels.data.option_chain import SwOptionChain
from stochvolmodels.pricers.factor_hjm.rate_factor_basis import NelsonSiegel
from stochvolmodels.pricers.factor_hjm.rate_logsv_params import MultiFactRateLogSvParams, TermStructure
from stochvolmodels.pricers.factor_hjm.rate_core import generate_ttms_grid, get_default_swap_term_structure
from stochvolmodels.pricers.factor_hjm.rate_logsv_pricer import simulate_logsv_MF, logsv_chain_de_pricer, Measure


def do_mc_simulation(basis_type: str,
                     ccy: str,
                     ttms: np.ndarray,
                     x0: np.ndarray,
                     y0: np.ndarray,
                     I0: np.ndarray,
                     sigma0: np.ndarray,
                     params: MultiFactRateLogSvParams,
                     nb_path: int,
                     seed: int = None,
                     measure_type: Measure = Measure.RISK_NEUTRAL,
                     ts_sw: np.ndarray = None,
                     bxs: np.ndarray = None,
                     year_days: int = 360,
                     T_fwd: float = None,
                     ) -> Tuple[List[np.ndarray], List[np.ndarray], List[np.ndarray], List[np.ndarray]]:
    if basis_type != "NELSON-SIEGEL" :
        raise NotImplementedError
    x0s, y0s, I0s, sigma0s = simulate_logsv_MF(ttms=ttms,
                                               x0=x0,
                                               y0=y0,
                                               I0=I0,
                                               sigma0=sigma0,
                                               theta=params.theta,
                                               kappa1=params.kappa1,
                                               kappa2=params.kappa2,
                                               ts=params.ts,
                                               A=params.A,
                                               R=params.R,
                                               C=params.C,
                                               Omega=params.Omega,
                                               betaxs=params.beta.xs,
                                               volvolxs=params.volvol.xs,
                                               basis=params.basis,
                                               measure_type=measure_type,
                                               nb_path=nb_path,
                                               seed=seed,
                                               ccy=ccy,
                                               ts_sw=ts_sw,
                                               T_fwd=T_fwd,
                                               params0=params,
                                               bxs=bxs,
                                               year_days = year_days)

    return x0s, y0s, I0s, sigma0s

def calc_mc_vols(basis_type: str,
                 params: MultiFactRateLogSvParams,
                 ttm: float,
                 tenors: np.ndarray,
                 forwards: List[np.ndarray],
                 strikes_ttms: List[List[np.ndarray]],
                 optiontypes: np.ndarray,
                 is_annuity_measure: bool,
                 nb_path: int,
                 x0: np.ndarray = None,
                 y0: np.ndarray = None,
                 sigma0: np.ndarray = None,
                 I0: np.ndarray = None,
                 seed: int = None,
                 x_in_delta_space: bool = False) -> (List[np.ndarray], List[np.ndarray]):
    # checks
    assert len(strikes_ttms) == len(tenors)
    assert len(strikes_ttms[0]) == 1
    assert len(forwards) == len(tenors)
    for fwd in forwards:
        assert fwd.shape == (1,)

    ttms = np.array([ttm])
    # we simulate under risk-neutral measure only
    assert is_annuity_measure is False
    if x0 is None:
        x0 = np.zeros((nb_path, params.basis.get_nb_factors()))
    else:
        assert x0.shape == (nb_path, params.basis.get_nb_factors(),)
    if y0 is None:
        y0 = np.zeros((nb_path, params.basis.get_nb_aux_factors()))
    else:
        assert y0.shape == (nb_path, params.basis.get_nb_aux_factors(),)
    if sigma0 is None:
        sigma0 = np.ones((nb_path, 1))
    else:
        assert sigma0.shape == (nb_path, 1)
    if I0 is None:
        I0 = np.zeros((nb_path,))
    else:
        assert I0.shape == (nb_path,)

    ts_sws = []
    bond0s = []
    ann0s = []
    swap0s = []
    for tenor in tenors:
        ts_sw = get_default_swap_term_structure(expiry=ttm, tenor=tenor)
        ann0 = params.basis.annuity(t=ttm, ts_sw=ts_sw, x=x0, y=y0, ccy=params.ccy, m=0)[0]
        bond0 = params.basis.bond(0, ttm, x=x0, y=y0, ccy=params.ccy, m=0)[0]
        swap0 = params.basis.swap_rate(t=ttm, ts_sw=ts_sw, x=x0, y=y0, ccy=params.ccy)[0][0]
        ts_sws.append(ts_sw)
        bond0s.append(bond0)
        ann0s.append(ann0)
        swap0s.append(swap0)

    x0s, y0s, I0s, _ = do_mc_simulation(basis_type=basis_type,
                                        ccy=params.ccy,
                                        ttms=ttms,
                                        x0=x0,
                                        y0=y0,
                                        I0=I0,
                                        sigma0=sigma0,
                                        params=params,
                                        nb_path=nb_path,
                                        seed=None,
                                        measure_type=Measure.RISK_NEUTRAL)
    x0 = x0s[-1]
    y0 = y0s[-1]
    I0 = I0s[-1]
    mc_vols = List()
    mc_prices = List()
    mc_vols_ups = List()
    mc_vols_downs = List()
    std_factor = 1.96

    for idx_tenor, tenor in enumerate(tenors):
        ts_sw = ts_sws[idx_tenor]
        ann0 = ann0s[idx_tenor]
        bond0 = bond0s[idx_tenor]
        swap0 = swap0s[idx_tenor]
        strikes_ttm = strikes_ttms[idx_tenor][0]
        swap_mc, ann_mc, numer_mc = params.basis.calculate_swap_rate(ttm=ttm, x0=x0, y0=y0, I0=I0, ts_sw=ts_sw,
                                                                     ccy=params.ccy)
        # calculate option payoffs
        payoffsign = np.where(optiontypes == 'P', -1, 1).astype(float)
        option_mean = np.zeros_like(strikes_ttm)
        option_std = np.zeros_like(strikes_ttm)

        for idx, (strike, sign) in enumerate(zip(strikes_ttm, payoffsign)):
            option_mean[idx] = np.nanmean(1. / numer_mc * ann_mc * np.maximum(sign * (swap_mc - strike), 0)) / ann0 / bond0
            option_std[idx] = np.nanstd(1. / numer_mc * ann_mc * np.maximum(sign * (swap_mc - strike), 0)) / ann0 / bond0
            option_std[idx] = option_std[idx] / np.sqrt(nb_path)

        option_up = option_mean + std_factor * option_std
        option_down = np.maximum(option_mean - std_factor * option_std, 0.0)

        mc_ivols_mid = bachel.infer_normal_ivols_from_chain_prices(ttms=ttms,
                                                                   forwards=forwards,
                                                                   discfactors=np.ones_like(ttms),
                                                                   strikes_ttms=[strikes_ttm],
                                                                   optiontypes_ttms=[optiontypes],
                                                                   model_prices_ttms=[option_mean])
        mc_ivols_up = bachel.infer_normal_ivols_from_chain_prices(ttms=ttms,
                                                                  forwards=forwards,
                                                                  discfactors=np.ones_like(ttms),
                                                                  strikes_ttms=[strikes_ttm],
                                                                  optiontypes_ttms=[optiontypes],
                                                                  model_prices_ttms=[option_up])
        mc_ivols_down = bachel.infer_normal_ivols_from_chain_prices(ttms=ttms,
                                                                    forwards=forwards,
                                                                    discfactors=np.ones_like(ttms),
                                                                    strikes_ttms=[strikes_ttm],
                                                                    optiontypes_ttms=[optiontypes],
                                                                    model_prices_ttms=[option_down])

        mc_vols.append(mc_ivols_mid[0])
        mc_vols_ups.append(mc_ivols_up[0])
        mc_vols_downs.append(mc_ivols_down[0])

        mc_prices.append(option_mean)

    return mc_prices, mc_vols, mc_vols_ups, mc_vols_downs


def plot_mkt_model_joint_smile_MF(swaption_chains: Dict[str, SwOptionChain],
                                  tenors: List[str],
                                  ttms_ids: List[str],
                                  params: Dict[str, MultiFactRateLogSvParams],
                                  x0: np.ndarray,
                                  y0: np.ndarray,
                                  slice_ids: List[str] = None,
                                  plot_market: bool = True) -> plt.Figure:
    nb_rows = len(swaption_chains.keys())
    ccy = "USD"
    nb_cols = params[ccy].basis.get_nb_factors()

    with sns.axes_style('darkgrid'):
        fig, axs = plt.subplots(1, nb_cols, figsize=(18, 5), tight_layout=True)


    swaption_chain = swaption_chains[ccy]
    idx_ttms = np.in1d(swaption_chain.ttms_ids, ttms_ids).nonzero()[0]
    ttms = np.array(swaption_chain.ttms)[range(np.max(idx_ttms)+1)]
    params0 = params[ccy]
    t_grid = generate_ttms_grid(ttms)
    palettes = ['blue', 'green', 'magenta', 'cyan', 'orange']

    for ttm, palette in zip(ttms, palettes):
        idx = np.where(swaption_chain.ttms == ttm)[0][0]
        idx_tenors = np.in1d(swaption_chain.tenors_ids, tenors).nonzero()[0]

        forwards = [swaption_chain.forwards[idx_tenor][[idx]] for idx_tenor, _ in enumerate(swaption_chain.tenors_ids)]
        strikes_ttms = [swaption_chain.strikes_ttms[idx_tenor][slice(idx, idx+1)] for idx_tenor, _ in enumerate(swaption_chain.tenors_ids)]
        optiontypes_ttms = [swaption_chain.optiontypes_ttms[idx]]

        model_prices_ttms, model_ivs_ttms = logsv_chain_de_pricer(params=params0,
                                                                  t_grid=t_grid,
                                                                  ttms=ttms[idx:idx+1],
                                                                  forwards=forwards,
                                                                  strikes_ttms=strikes_ttms,
                                                                  optiontypes_ttms=optiontypes_ttms,
                                                                  do_control_variate=False,
                                                                  is_stiff_solver=False,
                                                                  expansion_order=ExpansionOrder.FIRST,
                                                                  x0=x0,
                                                                  y0=y0)

        headers = ('(A)', '(B)', '(C)', '(D)', '(E)', '(F)')
        if ttms.size > 6:
            raise NotImplementedError(f"Extend header tags")
        else:
            headers = headers[:ttms.size]
        for idx_tenor, tenor_id in enumerate(tenors):
            ax = axs[idx_tenor]
            x_grid = bachel.strikes_to_delta(strikes=swaption_chain.strikes_ttms[idx_tenor][idx], ivols=swaption_chain.bid_ivs[idx_tenor][idx],
                                      f0=swaption_chain.forwards[idx_tenor][idx], ttm=ttm)
            mkt_ivols = pd.Series(swaption_chain.bid_ivs[idx_tenor][idx], index=x_grid, name=f"market").sort_index()
            mkt_ivols = SwOptionChain.remap_to_inc_delta(mkt_ivols)
            model_ivols = pd.Series(model_ivs_ttms[idx_tenor][0], index=x_grid, name=f"{swaption_chain.ttms_ids[idx]}: model").sort_index()
            model_ivols = SwOptionChain.remap_to_inc_delta(model_ivols)
            # ivols = pd.concat([mkt_ivols, model_ivols], axis=1)
            xvar_format = '{:.2%}'
            # ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda z, _: xvar_format.format(z)))
            # ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda z, _: xvar_format.format(z)))
            sns.lineplot(data=pd.concat([model_ivols], axis=1), ax=ax, palette=[palette], markers=False)
            if plot_market:
                sns.scatterplot(data=pd.concat([mkt_ivols], axis=1), ax=ax, palette=['red'])
            # report_ax_bps(ax=ax, xticks=swaption_chain.strikes_ttms[idx], y_axis_in_bps=True,
            #               y_label="Implied normal vols (bp)")
            # print(model_ivs_ttms)
    for idx_tenor, tenor_id in enumerate(tenors):
        ax = axs[idx_tenor]
        ax.set_xticks([-0.8, -0.65, -0.5, -0.35, -0.2])
        ax.set_xticklabels(['{:.2f}'.format(x, 2) for x in SwOptionChain.remap_to_pc_delta(ax.get_xticks())])
        ax.set_yticklabels(['{:.0f}'.format(x * 10000, 2) for x in ax.get_yticks()])
        title = f"{ccy}: {tenor_id} market data"
        ax.set_title(title, color='darkblue')
        h, l = ax.get_legend_handles_labels()
        ax.legend([*h[::2], h[-1]], [*l[::2], l[-1]], loc="upper left")

    return fig

class UnitTests(Enum):
    PLOT_MKT_MODEL = 5
    BENCHMARK_ANALYTIC_VS_MC = 8

def get_swaption_data(ccy: str = "USD") -> SwOptionChain:
    """
    swaption implied vol surface of 18 August 2023
    """
    ticker = 'USD_aug_23'
    ttms_ids = ['1y', '2y', '3y', '5y', '7y', '10y']
    ttms = np.array([1.0, 2.0, 3.0, 5.0, 7.0, 10.0])
    tenors = np.array([2.0, 5.0, 10.0])
    tenors_ids = ['2y', '5y', '10y']
    forwards = [np.array([4.0750, 4.0350, 4.0550, 4.1150, 4.1550, 4.1000]) * 0.01, np.array([4.0750, 4.0350, 4.0500, 4.1150, 4.1550, 4.1000]) * 0.01,
                np.array([4.0750, 4.0300, 4.0500, 4.1150, 4.1500, 4.1000]) * 0.01]
    ivs = [[np.array([164.82, 159.85, 156.28, 153.48, 151.6, 150.76, 151, 152.28, 154.51]) * 0.0001,
            np.array([137.84, 137.23, 137.64, 139.12, 141.67, 145.16, 149.44, 154.33, 159.7]) * 0.0001,
            np.array([123.88, 123.76, 124.84, 127.2, 130.75, 135.3, 140.61, 146.47, 152.7]) * 0.0001,
            np.array([109.39, 108.57, 109.15, 111.27, 114.8, 119.48, 124.97, 130.99, 137.34]) * 0.0001,
            np.array([99.54, 98.4, 98.57, 100.24, 103.34, 107.59, 112.66, 118.27, 124.2]) * 0.0001,
            np.array([90.59, 88.27, 87.23, 87.26, 90.24, 94.11, 99.04, 104.62, 110.57]) * 0.0001],
           [np.array([139.42, 136.82, 135.02, 134.17, 134.47, 135.62, 137.86, 140.94, 144.72]) * 0.0001,
            np.array([123.91, 122.97, 123.11, 124.43, 126.89, 130.35, 134.64, 139.55, 144.91]) * 0.0001,
            np.array([112.89, 112.6, 113.52, 115.7, 119.04, 123.33, 128.34, 133.86, 139.71]) * 0.0001,
            np.array([102.3, 101.56, 102.1, 104.02, 107.22, 111.46, 116.44, 121.92, 127.71]) * 0.0001,
            np.array([93.71, 92.57, 92.67, 94.16, 96.98, 100.9, 105.6, 110.81, 116.34]) * 0.0001,
            np.array([84.25, 82.31, 81.6, 82.41, 84.79, 88.48, 93.08, 98.26, 103.77]) * 0.0001],
           [np.array([116.41, 115.51, 115.54, 116.59, 118.62, 121.54, 125.2, 129.44, 134.11]) * 0.0001,
            np.array([108.04, 107.74, 108.47, 110.25, 113.03, 116.65, 120.93, 125.68, 130.78]) * 0.0001,
            np.array([101.43, 101.38, 102.35, 104.34, 107.29, 111.01, 115.32, 120.05, 125.07]) * 0.0001,
            np.array([91.69, 91.41, 92.33, 94.48, 97.72, 101.83, 106.54, 111.65, 117]) * 0.0001,
            np.array([84.28, 83.64, 84.33, 86.47, 89.89, 94.28, 99.32, 104.76, 110.4]) * 0.0001,
            np.array([74.54, 73.66, 74.14, 76.14, 79.51, 83.87, 88.87, 94.22, 99.75]) * 0.0001]]

    strikes_ttms = [[np.array([2.56, 2.93875, 3.3175, 3.69625, 4.075, 4.45375, 4.8325, 5.21125, 5.59]) * 0.01,
                     np.array([2.03, 2.53125, 3.0325, 3.53375, 4.035, 4.53625, 5.0375, 5.53875, 6.04]) * 0.01,
                     np.array([1.79, 2.35625, 2.9225, 3.48875, 4.055, 4.62125, 5.1875, 5.75375, 6.32]) * 0.01,
                     np.array([1.55, 2.19125, 2.8325, 3.47375, 4.115, 4.75625, 5.3975, 6.03875, 6.68]) * 0.01,
                     np.array([1.42, 2.10375, 2.7875, 3.47125, 4.155, 4.83875, 5.5225, 6.20625, 6.89]) * 0.01,
                     np.array([1.25, 1.9625, 2.675, 3.3875, 4.1, 4.8125, 5.525, 6.2375, 6.95]) * 0.01],
                    [np.array([2.73, 3.06625, 3.4025, 3.73875, 4.075, 4.41125, 4.7475, 5.08375, 5.42]) * 0.01,
                     np.array([2.24, 2.68875, 3.1375, 3.58625, 4.035, 4.48375, 4.9325, 5.38125, 5.83]) * 0.01,
                     np.array([1.99, 2.505, 3.02, 3.535, 4.05, 4.565, 5.08, 5.595, 6.11]) * 0.01,
                     np.array([1.72, 2.31875, 2.9175, 3.51625, 4.115, 4.71375, 5.3125, 5.91125, 6.51]) * 0.01,
                     np.array([1.59, 2.23125, 2.8725, 3.51375, 4.155, 4.79625, 5.4375, 6.07875, 6.72]) * 0.01,
                     np.array([1.42, 2.09, 2.76, 3.43, 4.1, 4.77, 5.44, 6.11, 6.78]) * 0.01],
                    [np.array([2.89, 3.18625, 3.4825, 3.77875, 4.075, 4.37125, 4.6675, 4.96375, 5.26]) * 0.01,
                     np.array([2.43, 2.83, 3.23, 3.63, 4.03, 4.43, 4.83, 5.23, 5.63]) * 0.01,
                     np.array([2.19, 2.655, 3.12, 3.585, 4.05, 4.515, 4.98, 5.445, 5.91]) * 0.01,
                     np.array([1.93, 2.47625, 3.0225, 3.56875, 4.115, 4.66125, 5.2075, 5.75375, 6.3]) * 0.01,
                     np.array([1.77, 2.365, 2.96, 3.555, 4.15, 4.745, 5.34, 5.935, 6.53]) * 0.01,
                     np.array([1.59, 2.2175, 2.845, 3.4725, 4.1, 4.7275, 5.355, 5.9825, 6.61]) * 0.01]]

    chain = SwOptionChain.create_swaption_chain_MF(ccy=ccy,
                                                   tenors=tenors,
                                                   tenors_ids=tenors_ids,
                                                   ttms=ttms,
                                                   ttms_ids=ttms_ids,
                                                   forwards=forwards,
                                                   strikes_ttms=strikes_ttms,
                                                   ivs=ivs,
                                                   ticker=ticker)

    return chain


def getCalibRateLogSVParams(type_str: str = "NELSON-SIEGEL") -> Dict[str, MultiFactRateLogSvParams]:
    """return dictionary of parameters, per currency"""
    dict = {}

    ttms = np.array([1.0, 2.0, 3.0, 5.0])
    # R_corr = np.array([[1.0, 0.95, 0.8, 0.7], [0.95, 1.0, 0.9, 0.7], [0.9, 0.9, 1.0, 0.9], [0.7, 0.7, 0.9, 1.0]])
    # R_corr = np.array([[1.0, 0.99, 0.97], [0.99, 1.0, 0.98], [0.97, 0.98, 1.0]])
    R_corr = np.array([[1.0, 0.99, 0.97], [0.99, 1.0, 0.98], [0.97, 0.98, 1.0]])
    # R_corr = np.array([[1.0, 0.98], [0.98, 1.0]])
    # R_corr = np.array([[1.0, 0.9, 0.7], [0.9, 1.0, 0.8], [0.7, 0.8, 1.0]])
    # R_corr = np.array([[1.0, 0.7, -0.3], [0.7, 1.0, 0.4], [-0.3, 0.4, 1.0]])

    if type_str == "NELSON-SIEGEL":
        nelson_siegel = NelsonSiegel(meanrev=0.55, key_terms=np.array([2.0, 5.0, 10.0]))
        nb_factors = NelsonSiegel.get_nb_factors()
        times = np.concatenate((0, ttms), axis=None)

        params0 = MultiFactRateLogSvParams(
            sigma0=1.0, theta=1.0, kappa1=0.25, kappa2=0.25,
            beta=TermStructure.create_multi_fact_from_vec(times, np.array([0.2, 0.2, 0.2])),
            volvol=TermStructure.create_from_scalar(times, 0.2),
            A=np.array([0.01, 0.01, 0.01]),
            R=R_corr,
            basis=nelson_siegel,
            ccy="USD", vol_interpolation="BY_YIELD")

        params0.update_params(idx=0,
                              A_idx=np.array([0.0145520600966057, 0.0129872854900715, 0.0113053431415981]),
                              beta_idx=np.array([1.5175197006627835e-02,  1.0634920321914283e-01,  6.6674118846722419e-01]),
                              volvol_idx=0.0972782445446557)
        params0.update_params(idx=1,
                              A_idx=np.array([0.0134748570248017, 0.0128907769293694, 0.0112651548589306]),
                              beta_idx=np.array([4.8368206184131085e-01,  1.7547946297795609e-02, -2.8323520431018540e-01]),
                              volvol_idx=0.1071198215096482)
        params0.update_params(idx=2,
                              A_idx=np.array([0.011573352659394,  0.0122196017111508, 0.010764379038105]),
                              beta_idx=np.array([6.5149765993861006e-02, -8.1944955908784672e-02, -1.2933054838433659e-04]),
                              volvol_idx=0.0744932897602731)
        params0.update_params(idx=3,
                              A_idx=np.array([0.0070554411390967, 0.0097915826853067, 0.0086699569420959]),
                              beta_idx=np.array([4.0771895182424006e-01, -7.2998068741307848e-02, -4.0049869808018973e-01]),
                              volvol_idx=0.03)

        dict["USD"] = params0

    else:
        raise NotImplementedError
    return dict


def run_unit_test(unit_test: UnitTests):
    if unit_test == UnitTests.BENCHMARK_ANALYTIC_VS_MC:
        curr = "USD"
        swaption_chain = get_swaption_data(curr)
        basis_type = "NELSON-SIEGEL"
        ids = ['1y', '2y', '3y', '5y']  # <-- change here
        params0 = getCalibRateLogSVParams(basis_type)[curr]
        params0.q = params0.theta
        # params0 = params0.reduce(ids)  # <-- change here

        swaption_chain = swaption_chain.reduce_tenors(['2y', '5y', '10y']).reduce_strikes(2)
        swaption_chain = swaption_chain.reduce_ttms(ids)

        assert np.all(swaption_chain.tenors == params0.basis.key_terms)

        # ttm = MultiFactRateLogSvParams.get_frac(ids[0])
        nb_path = 50000

        ttm_id = '5y'
        assert ttm_id in swaption_chain.tenors_ids
        swaption_chain = swaption_chain.reduce_ttms(['5y'])
        ttm = swaption_chain.ttms[-1]

        strikes_ttms_mc = [[np.linspace(strikes[0], strikes[-1], 21) for strikes in strikes_ttm] for strikes_ttm in
                        swaption_chain.strikes_ttms]
        optiontypes_ttms = np.repeat('C', strikes_ttms_mc[0][0].size)

        # x0 = 0.01 * np.array([4.36, 1.3, -1.0])
        # y0 = np.zeros((nb_path, params0.basis.get_nb_aux_factors()))

        mc_ivols, mc_ivols_ups, mc_ivols_downs = calc_mc_vols(
            basis_type=basis_type,
            params=params0,
            ttm=swaption_chain.ttms[-1],
            tenors=swaption_chain.tenors,
            forwards=swaption_chain.forwards,
            strikes_ttms=strikes_ttms_mc,
            optiontypes=optiontypes_ttms,
            is_annuity_measure=False,
            nb_path=nb_path,
            sigma0=None,
            I0=None)[1:]
        # print(mc_ivols)

        ttms = np.array([ttm])
        t_grid = generate_ttms_grid(ttms)

        # x0 = 0.01 * np.array([4.36, 1.3, -1.0])
        # y0 = np.zeros((params0.basis.get_nb_aux_factors(),))
        x0 = None
        y0 = None

        model_prices_ttms, model_ivs_ttms = logsv_chain_de_pricer(params=params0,
                                                                  t_grid=t_grid,
                                                                  ttms=ttms,
                                                                  forwards=swaption_chain.forwards,
                                                                  strikes_ttms=swaption_chain.strikes_ttms,
                                                                  optiontypes_ttms=swaption_chain.optiontypes_ttms,
                                                                  do_control_variate=False,
                                                                  is_stiff_solver=False,
                                                                  expansion_order=ExpansionOrder.FIRST,
                                                                  x0=x0,
                                                                  y0=y0)
        # print(model_ivs_ttms)

        nb_cols = swaption_chain.tenors.size
        with sns.axes_style('darkgrid'):
            fig, axs = matplotlib.pyplot.subplots(1, nb_cols, figsize=(18, 4), tight_layout=True)

        for idx, tenor in enumerate(swaption_chain.tenors):
            ax = axs[idx] if nb_cols > 1 else axs
            model_ivols_pd = pd.Series(model_ivs_ttms[idx][0], index=swaption_chain.strikes_ttms[idx][0], name=f"Affine expansion")
            # sns.scatterplot(data=pd.concat([mc_ivols_pd], axis=1), ax=ax, color='green')
            df = pd.DataFrame(np.array([mc_ivols_ups[idx], mc_ivols_downs[idx]]).T,
                              index=strikes_ttms_mc[idx][0], columns=[f"MC+0.95ci", f"MC-0.95ci"])
            sns.scatterplot(data=df, palette=['green', 'red'], markers=[7, 6], ax=ax)
            sns.lineplot(data=pd.concat([model_ivols_pd], axis=1), ax=ax, color='blue')
            title = f"{curr}: {swaption_chain.tenors_ids[idx]} market data"
            ax.set_title(title, color='darkblue')
            ax.set_xticklabels(['{:.0f}'.format(x * 10000, 2) for x in ax.get_xticks()])
            ax.set_yticklabels(['{:.0f}'.format(x * 10000, 2) for x in ax.get_yticks()])

    elif unit_test == UnitTests.PLOT_MKT_MODEL:
        curr = "USD"
        swaption_chain = get_swaption_data()
        basis_type = "NELSON-SIEGEL"
        # basis_type = "PE-ND"
        ids = ['1y', '2y', '3y', '5y']  # <-- change here
        swaption_chain = swaption_chain.reduce_tenors(['2y', '5y', '10y']).reduce_strikes(2)
        swaption_chain = swaption_chain.reduce_ttms(ids)
        swaption_chains = {curr: swaption_chain}

        params0 = getCalibRateLogSVParams(basis_type)[curr]
        params0.q = params0.theta
        # for idx in [0, 1, 2, 3]:
        #     params0.update_params(idx=idx,
        #                           A_idx=np.array([0.01, 0.01, 0.01]),
        #                           beta_idx=np.array([0.1, 0.1, -0.2]),
        #                           volvol_idx=0.2)
        params0 = params0.reduce(ids)  # <-- change here

        for ttm in params0.ts[1:]:
            for tenor in swaption_chain.tenors:
                assert params0.check_QA_kappa2(expiry=ttm, tenor=tenor)


        params = {curr: params0}
        x0 = np.array([0.0, 0.0, 0.0])
        y0 = np.zeros((params0.basis.get_nb_aux_factors()))
        fig = plot_mkt_model_joint_smile_MF(swaption_chains=swaption_chains, ttms_ids=ids, params=params,
                                            tenors=swaption_chain.tenors_ids,
                                            slice_ids=swaption_chains["USD"].ttms_ids,
                                            x0=x0, y0=y0)
        # fig.savefig(f"..//draft//figures//fhjm//calibration_FHJM_swaptions.pdf")

    plt.show()


if __name__ == '__main__':

    unit_test = UnitTests.BENCHMARK_ANALYTIC_VS_MC


    is_run_all_tests = True
    if is_run_all_tests:
        for unit_test in UnitTests:
            run_unit_test(unit_test=unit_test)
    else:
        run_unit_test(unit_test=unit_test)