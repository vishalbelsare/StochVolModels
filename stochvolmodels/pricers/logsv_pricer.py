"""
Implementation of log-normal stochastic volatility model
The lognormal sv model interface derives from ModelPricer
"""
# package
import numpy as np
import pandas as pd
from numba import njit
from numba.typed import List
from typing import Tuple, Optional
from scipy.optimize import minimize
from enum import Enum

# stochvolmodels
from stochvolmodels.utils.config import VariableType
import stochvolmodels.utils.mgf_pricer as mgfp
from stochvolmodels.utils.mc_payoffs import compute_mc_vars_payoff
from stochvolmodels.utils.funcs import to_flat_np_array, set_time_grid, timer, compute_histogram_data, set_seed, pos

# stochvolmodels pricers
from stochvolmodels.pricers.logsv.logsv_params import LogSvParams
import stochvolmodels.pricers.logsv.affine_expansion as afe
from stochvolmodels.pricers.model_pricer import ModelPricer
from stochvolmodels.pricers.logsv.affine_expansion import ExpansionOrder
from stochvolmodels.pricers.logsv.vol_moments_ode import fit_model_vol_backbone_to_varswaps
from stochvolmodels.pricers.logsv.rough_kernel_approx import approximat_kernel

# data
from stochvolmodels.data.option_chain import OptionChain
from stochvolmodels.data.test_option_chain import get_btc_test_chain_data


class LogsvModelCalibrationType(Enum):
    PARAMS4 = 1  # v0, theta, beta, volvol; kappa1, kappa2 are set externally
    PARAMS5 = 2  # v0, theta, kappa1, beta, volvol
    PARAMS6 = 3  # v0, theta, kappa1, kappa2, beta, volvol
    PARAMS_WITH_VARSWAP_FIT = 4  # beta, volvol; kappa1, kappa2 are set externally; term structure of varswap is fit


class ConstraintsType(Enum):
    UNCONSTRAINT = 1
    MMA_MARTINGALE = 2  # kappa_2 >= beta
    INVERSE_MARTINGALE = 3  # kappa_2 >= 2.0*beta
    MMA_MARTINGALE_MOMENT4 = 4  # kappa_2 >= beta &
    INVERSE_MARTINGALE_MOMENT4 = 5  # kappa_2 >= 2.0*beta


class CalibrationEngine(Enum):
    ANALYTIC = 1
    MC = 2


LOGSV_BTC_PARAMS = LogSvParams(sigma0=0.8376, theta=1.0413, kappa1=3.1844, kappa2=3.058, beta=0.1514, volvol=1.8458)


class LogSVPricer(ModelPricer):

    # @timer
    def price_chain(self,
                    option_chain: OptionChain,
                    params: LogSvParams,
                    is_spot_measure: bool = True,
                    **kwargs
                    ) -> List[np.ndarray]:
        """
        implementation of generic method price_chain using log sv wrapper
        """
        model_prices = logsv_chain_pricer(params=params,
                                          ttms=option_chain.ttms,
                                          forwards=option_chain.forwards,
                                          discfactors=option_chain.discfactors,
                                          strikes_ttms=option_chain.strikes_ttms,
                                          optiontypes_ttms=option_chain.optiontypes_ttms,
                                          is_spot_measure=is_spot_measure,
                                          **kwargs)
        return model_prices

    @timer
    def model_mc_price_chain(self,
                             option_chain: OptionChain,
                             params: LogSvParams,
                             is_spot_measure: bool = True,
                             variable_type: VariableType = VariableType.LOG_RETURN,
                             nb_path: int = 100000,
                             nb_steps: Optional[int] = None,
                             **kwargs
                             ) -> (List[np.ndarray], List[np.ndarray]):
        vol_backbone_etas = params.get_vol_backbone_etas(ttms=option_chain.ttms)
        return logsv_mc_chain_pricer(v0=params.sigma0,
                                     theta=params.theta,
                                     kappa1=params.kappa1,
                                     kappa2=params.kappa2,
                                     beta=params.beta,
                                     volvol=params.volvol,
                                     vol_backbone_etas=vol_backbone_etas,
                                     ttms=option_chain.ttms,
                                     forwards=option_chain.forwards,
                                     discfactors=option_chain.discfactors,
                                     strikes_ttms=option_chain.strikes_ttms,
                                     optiontypes_ttms=option_chain.optiontypes_ttms,
                                     is_spot_measure=is_spot_measure,
                                     variable_type=variable_type,
                                     nb_path=nb_path,
                                     nb_steps_per_year=nb_steps or int(360 * np.max(option_chain.ttms)) + 1)

    def set_vol_scaler(self, option_chain: OptionChain) -> float:
        """
        use chain vols to set the scaler
        """
        atm0 = option_chain.get_chain_atm_vols()[0]
        ttm0 = option_chain.ttms[0]
        return set_vol_scaler(sigma0=atm0, ttm=ttm0)

    @timer
    def calibrate_model_params_to_chain(self,
                                        option_chain: OptionChain,
                                        params0: LogSvParams,
                                        params_min: LogSvParams = LogSvParams(sigma0=0.1, theta=0.1, kappa1=0.25,
                                                                              kappa2=0.25, beta=-3.0, volvol=0.2),
                                        params_max: LogSvParams = LogSvParams(sigma0=1.5, theta=1.5, kappa1=10.0,
                                                                              kappa2=10.0, beta=3.0, volvol=3.0),
                                        is_vega_weighted: bool = True,
                                        is_unit_ttm_vega: bool = False,
                                        model_calibration_type: LogsvModelCalibrationType = LogsvModelCalibrationType.PARAMS5,
                                        constraints_type: ConstraintsType = ConstraintsType.UNCONSTRAINT,
                                        calibration_engine: CalibrationEngine = CalibrationEngine.ANALYTIC,
                                        nb_path: int = 100000,
                                        nb_steps: int = 360,
                                        seed: int = 10,
                                        **kwargs
                                        ) -> LogSvParams:
        """
        implementation of model calibration interface with nonlinear constraints
        """
        vol_scaler = self.set_vol_scaler(option_chain=option_chain)

        x, market_vols = option_chain.get_chain_data_as_xy()
        market_vols = to_flat_np_array(market_vols)  # market mid quotes

        if is_vega_weighted:
            vegas_ttms = option_chain.get_chain_vegas(is_unit_ttm_vega=is_unit_ttm_vega)
            # if is_unit_ttm_vega:
            vegas_ttms = [vegas_ttm / sum(vegas_ttm) for vegas_ttm in vegas_ttms]
            weights = to_flat_np_array(vegas_ttms)
        else:
            weights = np.ones_like(market_vols)

        if model_calibration_type == LogsvModelCalibrationType.PARAMS_WITH_VARSWAP_FIT:
            varswap_strikes = option_chain.get_slice_varswap_strikes(floor_with_atm_vols=True)
        else:
            varswap_strikes = None

        def parse_model_params(pars: np.ndarray) -> LogSvParams:
            if model_calibration_type == LogsvModelCalibrationType.PARAMS4:
                fit_params = LogSvParams(sigma0=pars[0],
                                         theta=pars[1],
                                         kappa1=params0.kappa1,
                                         kappa2=params0.kappa2,
                                         beta=pars[2],
                                         volvol=pars[3])
            elif model_calibration_type == LogsvModelCalibrationType.PARAMS5:
                fit_params = LogSvParams(sigma0=pars[0],
                                         theta=pars[1],
                                         kappa1=pars[2],
                                         kappa2=None,
                                         beta=pars[3],
                                         volvol=pars[4])
            elif model_calibration_type == LogsvModelCalibrationType.PARAMS_WITH_VARSWAP_FIT:
                fit_params = LogSvParams(sigma0=params0.sigma0,
                                         theta=params0.theta,
                                         kappa1=params0.kappa1,
                                         kappa2=params0.kappa2,
                                         beta=pars[0],
                                         volvol=pars[1])
                # set model backbone
                vol_backbone = fit_model_vol_backbone_to_varswaps(log_sv_params=fit_params,
                                                                  varswap_strikes=varswap_strikes)
                fit_params.set_vol_backbone(vol_backbone=vol_backbone)

            else:
                raise NotImplementedError(f"{model_calibration_type}")
            return fit_params

        if calibration_engine == CalibrationEngine.MC:
            W0s, W1s, dts = get_randoms_for_chain_valuation(ttms=option_chain.ttms, nb_path=nb_path,
                                                            nb_steps_per_year=nb_steps, seed=seed)

        def objective(pars: np.ndarray, args: np.ndarray) -> float:
            params = parse_model_params(pars=pars)

            if calibration_engine == CalibrationEngine.ANALYTIC:
                model_vols = self.compute_model_ivols_for_chain(option_chain=option_chain, params=params,
                                                                vol_scaler=vol_scaler)

            elif calibration_engine == CalibrationEngine.MC:
                option_prices_ttm, option_std_ttm = logsv_mc_chain_pricer_fixed_randoms(ttms=option_chain.ttms,
                                                                                        forwards=option_chain.forwards,
                                                                                        discfactors=option_chain.discfactors,
                                                                                        strikes_ttms=option_chain.strikes_ttms,
                                                                                        optiontypes_ttms=option_chain.optiontypes_ttms,
                                                                                        W0s=W0s,
                                                                                        W1s=W1s,
                                                                                        dts=dts,
                                                                                        v0=params.sigma0,
                                                                                        theta=params.theta,
                                                                                        kappa1=params.kappa1,
                                                                                        kappa2=params.kappa2,
                                                                                        beta=params.beta,
                                                                                        volvol=params.volvol,
                                                                                        vol_backbone_etas=params.get_vol_backbone_etas(
                                                                                            ttms=option_chain.ttms))
                model_vols = option_chain.compute_model_ivols_from_chain_data(model_prices=option_prices_ttm)
                # print(f"option_prices_ttm\n{option_prices_ttm}")
                # print(f"model_vols\n{model_vols}")

            else:
                raise NotImplementedError(f"{calibration_engine}")

            resid = np.nansum(weights * np.square(to_flat_np_array(model_vols) - market_vols))
            return resid

        # parametric constraints
        def martingale_measure(pars: np.ndarray) -> float:
            params = parse_model_params(pars=pars)
            return params.kappa2 - params.beta

        def inverse_measure(pars: np.ndarray) -> float:
            params = parse_model_params(pars=pars)
            return params.kappa2 - 2.0 * params.beta

        def vol_4thmoment_finite(pars: np.ndarray) -> float:
            params = parse_model_params(pars=pars)
            kappa = params.kappa1 + params.kappa2 * params.theta
            return kappa - 1.5 * params.vartheta2

        # set initial params
        if model_calibration_type == LogsvModelCalibrationType.PARAMS4:
            # fit: v0, theta, beta, volvol; kappa1, kappa2 is given with params0
            p0 = np.array([params0.sigma0, params0.theta, params0.beta, params0.volvol])
            bounds = ((params_min.sigma0, params_max.sigma0),
                      (params_min.theta, params_max.theta),
                      (params_min.beta, params_max.beta),
                      (params_min.volvol, params_max.volvol))

        elif model_calibration_type == LogsvModelCalibrationType.PARAMS5:
            # fit: v0, theta, kappa1, beta, volvol; kappa2 is mapped as kappa1 / theta
            p0 = np.array([params0.sigma0, params0.theta, params0.kappa1, params0.beta, params0.volvol])
            bounds = ((params_min.sigma0, params_max.sigma0),
                      (params_min.theta, params_max.theta),
                      (params_min.kappa1, params_max.kappa1),
                      (params_min.beta, params_max.beta),
                      (params_min.volvol, params_max.volvol))

        elif model_calibration_type == LogsvModelCalibrationType.PARAMS_WITH_VARSWAP_FIT:
            # beta, volvol; kappa1, kappa2 are set externally; term structure of varswap is fit
            p0 = np.array([params0.beta, params0.volvol])
            bounds = ((params_min.beta, params_max.beta),
                      (params_min.volvol, params_max.volvol))
        else:
            raise NotImplementedError(f"{model_calibration_type}")

        options = {'disp': True, 'ftol': 1e-8}

        if constraints_type == ConstraintsType.UNCONSTRAINT:
            constraints = None
        elif constraints_type == ConstraintsType.MMA_MARTINGALE:
            constraints = ({'type': 'ineq', 'fun': martingale_measure})
        elif constraints_type == ConstraintsType.INVERSE_MARTINGALE:
            constraints = ({'type': 'ineq', 'fun': inverse_measure})
        elif constraints_type == ConstraintsType.MMA_MARTINGALE_MOMENT4:
            constraints = ({'type': 'ineq', 'fun': martingale_measure}, {'type': 'ineq', 'fun': vol_4thmoment_finite})
        elif constraints_type == ConstraintsType.INVERSE_MARTINGALE_MOMENT4:
            constraints = ({'type': 'ineq', 'fun': inverse_measure}, {'type': 'ineq', 'fun': vol_4thmoment_finite})
        else:
            raise NotImplementedError

        """
        match constraints_type:
            case ConstraintsType.UNCONSTRAINT:
                constraints = None
            case ConstraintsType.MMA_MARTINGALE:
                constraints = ({'type': 'ineq', 'fun': martingale_measure})
            case ConstraintsType.INVERSE_MARTINGALE:
                constraints = ({'type': 'ineq', 'fun': inverse_measure})
            case ConstraintsType.MMA_MARTINGALE_MOMENT4:
                constraints = ({'type': 'ineq', 'fun': martingale_measure}, {'type': 'ineq', 'fun': vol_4thmoment_finite})
            case ConstraintsType.INVERSE_MARTINGALE_MOMENT4:
                constraints = ({'type': 'ineq', 'fun': inverse_measure}, {'type': 'ineq', 'fun': vol_4thmoment_finite})
            case _:
                raise NotImplementedError
        """
        if constraints is not None:
            res = minimize(objective, p0, args=None, method='SLSQP', constraints=constraints, bounds=bounds,
                           options=options)
        else:
            res = minimize(objective, p0, args=None, method='SLSQP', bounds=bounds, options=options)

        fit_params = parse_model_params(pars=res.x)

        return fit_params

    @timer
    def simulate_vol_paths(self,
                           params: LogSvParams,
                           brownians: np.ndarray = None,
                           ttm: float = 1.0,
                           nb_path: int = 100000,
                           is_spot_measure: bool = True,
                           nb_steps: int = None,
                           year_days: int = 360,
                           **kwargs
                           ) -> Tuple[np.ndarray, np.ndarray]:
        """
        simulate vols in dt_path grid
        """
        nb_steps = nb_steps or int(np.ceil(year_days * ttm))
        if params.H >= 0.5 - 1e-4:
            sigma_t, grid_t = simulate_vol_paths(ttm=ttm,
                                                 v0=params.sigma0,
                                                 theta=params.theta,
                                                 kappa1=params.kappa1,
                                                 kappa2=params.kappa2,
                                                 beta=params.beta,
                                                 volvol=params.volvol,
                                                 nb_path=nb_path,
                                                 is_spot_measure=is_spot_measure,
                                                 nb_steps_per_year=nb_steps,
                                                 brownians=brownians,
                                                 **kwargs)
        elif 0.0 < params.H < 0.5 - 1e-4:
            sigma_t, grid_t = simulate_roughvol_paths(ttm=ttm,
                                                      v0=params.sigma0,
                                                      theta=params.theta,
                                                      kappa1=params.kappa1,
                                                      kappa2=params.kappa2,
                                                      beta=params.beta,
                                                      volvol=params.volvol,
                                                      H=params.H,
                                                      nb_path=nb_path,
                                                      is_spot_measure=is_spot_measure,
                                                      nb_steps_per_year=nb_steps,
                                                      Z=brownians,
                                                      **kwargs)
        else:
            raise NotImplementedError("Wrong value for H")
        return sigma_t, grid_t

    @timer
    def simulate_terminal_values(self,
                                 params: LogSvParams,
                                 ttm: float = 1.0,
                                 nb_path: int = 100000,
                                 is_spot_measure: bool = True,
                                 **kwargs
                                 ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        simulate terminal values
        """
        x0, sigma0, qvar0 = simulate_logsv_x_vol_terminal(ttm=ttm,
                                                          x0=np.zeros(nb_path),
                                                          sigma0=params.sigma0 * np.ones(nb_path),
                                                          qvar0=np.zeros(nb_path),
                                                          theta=params.theta,
                                                          kappa1=params.kappa1,
                                                          kappa2=params.kappa2,
                                                          beta=params.beta,
                                                          volvol=params.volvol,
                                                          nb_path=nb_path,
                                                          is_spot_measure=is_spot_measure)
        return x0, sigma0, qvar0

    @timer
    def logsv_pdfs(self,
                   params: LogSvParams,
                   ttm: float,
                   space_grid: np.ndarray,
                   is_stiff_solver: bool = False,
                   is_analytic: bool = False,
                   is_spot_measure: bool = True,
                   expansion_order: ExpansionOrder = ExpansionOrder.SECOND,
                   variable_type: VariableType = VariableType.LOG_RETURN,
                   vol_scaler: float = None
                   ) -> np.ndarray:
        return logsv_pdfs(params=params,
                          ttm=ttm,
                          space_grid=space_grid,
                          is_stiff_solver=is_stiff_solver,
                          is_analytic=is_analytic,
                          is_spot_measure=is_spot_measure,
                          expansion_order=expansion_order,
                          variable_type=variable_type,
                          vol_scaler=vol_scaler
                          )


@njit(cache=False, fastmath=True)
def v0_implied(atm: float, beta: float, volvol: float, theta: float, kappa1: float, ttm: float):
    """
    approximation for short term model atm vol
    """
    beta2 = beta * beta
    volvol2 = volvol * volvol
    vartheta2 = beta2 + volvol2

    def simple():
        return atm - (beta2 + volvol2) * ttm / 4.0

    if np.abs(beta) > 1.0:  # cannot use approximation when beta is too high
        v0 = simple()
    else:
        numer = -24.0 - beta2 * ttm - 2.0 * vartheta2 * ttm + 12.0 * kappa1 * ttm + np.sqrt(
            np.square(24.0 + beta2 * ttm + 2.0 * vartheta2 * ttm - 12.0 * kappa1 * ttm) - 288.0 * beta * ttm * (
                    -2.0 * atm + theta * kappa1 * ttm))
        denumer = 12.0 * beta * ttm
        if np.abs(denumer) > 1e-10:
            v0 = numer / denumer
        else:
            v0 = atm - (np.square(beta) + np.square(volvol)) * ttm / 4.0
    return v0


def set_vol_scaler(sigma0: float, ttm: float) -> float:
    return sigma0 * np.sqrt(np.minimum(np.min(ttm), 0.5 / 12.0))  # lower bound is two weeks


def logsv_chain_pricer(params: LogSvParams,
                       ttms: np.ndarray,
                       forwards: np.ndarray,
                       discfactors: np.ndarray,
                       strikes_ttms: List[np.ndarray],
                       optiontypes_ttms: List[np.ndarray],
                       is_stiff_solver: bool = False,
                       is_analytic: bool = False,
                       is_spot_measure: bool = True,
                       expansion_order: ExpansionOrder = ExpansionOrder.SECOND,
                       variable_type: VariableType = VariableType.LOG_RETURN,
                       vol_scaler: float = None,
                       **kwargs
                       ) -> List[np.ndarray]:
    """
    wrapper to price option chain on variable_type
    to do: do numba implementation using numba consistent solver
    """
    # starting values
    if vol_scaler is None:  # for calibrations we fix one vol_scaler so the grid is not affected by v0
        vol_scaler = set_vol_scaler(sigma0=params.sigma0, ttm=np.min(ttms))

    phi_grid, psi_grid, theta_grid = mgfp.get_transform_var_grid(variable_type=variable_type,
                                                                 is_spot_measure=is_spot_measure,
                                                                 vol_scaler=vol_scaler)

    a_t0 = np.zeros((phi_grid.shape[0], afe.get_expansion_n(expansion_order)), dtype=np.complex128)
    ttm0 = 0.0

    # outputs as numpy lists
    model_prices_ttms = List()
    for ttm, forward, strikes_ttm, optiontypes_ttm, discfactor in zip(ttms, forwards, strikes_ttms, optiontypes_ttms,
                                                                      discfactors):
        vol_backbone_eta = params.get_vol_backbone_eta(tau=ttm)
        a_t0, log_mgf_grid = afe.compute_logsv_a_mgf_grid(ttm=ttm - ttm0,
                                                          phi_grid=phi_grid,
                                                          psi_grid=psi_grid,
                                                          theta_grid=theta_grid,
                                                          a_t0=a_t0,
                                                          is_analytic=is_analytic,
                                                          expansion_order=expansion_order,
                                                          is_stiff_solver=is_stiff_solver,
                                                          is_spot_measure=is_spot_measure,
                                                          **params.to_dict(),
                                                          vol_backbone_eta=vol_backbone_eta)

        if variable_type == VariableType.LOG_RETURN:
            option_prices = mgfp.vanilla_slice_pricer_with_mgf_grid(log_mgf_grid=log_mgf_grid,
                                                                    phi_grid=phi_grid,
                                                                    forward=forward,
                                                                    strikes=strikes_ttm,
                                                                    optiontypes=optiontypes_ttm,
                                                                    discfactor=discfactor,
                                                                    is_spot_measure=is_spot_measure)

        elif variable_type == VariableType.Q_VAR:
            option_prices = mgfp.slice_qvar_pricer_with_a_grid(log_mgf_grid=log_mgf_grid,
                                                               psi_grid=psi_grid,
                                                               ttm=ttm,
                                                               forward=forward,
                                                               strikes=strikes_ttm,
                                                               optiontypes=optiontypes_ttm,
                                                               discfactor=discfactor,
                                                               is_spot_measure=is_spot_measure)

        else:
            raise NotImplementedError

        model_prices_ttms.append(option_prices)
        ttm0 = ttm

    return model_prices_ttms


def logsv_pdfs(params: LogSvParams,
               ttm: float,
               space_grid: np.ndarray,
               is_stiff_solver: bool = False,
               is_analytic: bool = False,
               is_spot_measure: bool = True,
               expansion_order: ExpansionOrder = ExpansionOrder.SECOND,
               variable_type: VariableType = VariableType.LOG_RETURN,
               vol_scaler: float = None
               ) -> np.ndarray:
    """
    wrapper to compute model pdfs
    """
    # starting values
    if vol_scaler is None:  # for calibrations we fix one vol_scaler so the grid is not affected by v0
        vol_scaler = set_vol_scaler(sigma0=params.sigma0, ttm=ttm)

    phi_grid, psi_grid, theta_grid = mgfp.get_transform_var_grid(variable_type=variable_type,
                                                                 is_spot_measure=is_spot_measure,
                                                                 vol_scaler=vol_scaler)

    a_t0 = afe.get_init_conditions_a(phi_grid=phi_grid,
                                     psi_grid=psi_grid,
                                     theta_grid=theta_grid,
                                     n_terms=afe.get_expansion_n(expansion_order=expansion_order),
                                     variable_type=variable_type)

    # compute mgf
    a_t0, log_mgf_grid = afe.compute_logsv_a_mgf_grid(ttm=ttm,
                                                      phi_grid=phi_grid,
                                                      psi_grid=psi_grid,
                                                      theta_grid=theta_grid,
                                                      a_t0=a_t0,
                                                      is_analytic=is_analytic,
                                                      expansion_order=expansion_order,
                                                      is_stiff_solver=is_stiff_solver,
                                                      is_spot_measure=is_spot_measure,
                                                      **params.to_dict())

    # outputs as numpy lists
    if variable_type == VariableType.LOG_RETURN:
        transform_var_grid = phi_grid
        shift = 0.0
        scale = 1.0
    elif variable_type == VariableType.Q_VAR:  # scaled by ttm
        transform_var_grid = psi_grid
        shift = 0.0
        scale = 1.0 / ttm
    elif variable_type == VariableType.SIGMA:
        transform_var_grid = theta_grid
        shift = params.theta
        scale = 1.0
    else:
        raise NotImplementedError

    pdf = mgfp.pdf_with_mgf_grid(log_mgf_grid=log_mgf_grid,
                                 transform_var_grid=transform_var_grid,
                                 space_grid=space_grid,
                                 shift=shift,
                                 scale=scale)
    pdf = pdf / scale
    return pdf


@njit(cache=False, fastmath=True)
def logsv_mc_chain_pricer(ttms: np.ndarray,
                          forwards: np.ndarray,
                          discfactors: np.ndarray,
                          strikes_ttms: Tuple[np.ndarray, ...],
                          optiontypes_ttms: Tuple[np.ndarray, ...],
                          v0: float,
                          theta: float,
                          kappa1: float,
                          kappa2: float,
                          beta: float,
                          volvol: float,
                          vol_backbone_etas: np.ndarray,
                          is_spot_measure: bool = True,
                          nb_path: int = 100000,
                          nb_steps_per_year: int = 360,
                          variable_type: VariableType = VariableType.LOG_RETURN
                          ) -> Tuple[List[np.ndarray], List[np.ndarray]]:
    # starting values
    x0 = np.zeros(nb_path)
    qvar0 = np.zeros(nb_path)
    sigma0 = v0 * np.ones(nb_path)
    ttm0 = 0.0

    # outputs as numpy lists
    option_prices_ttm = List()
    option_std_ttm = List()
    for ttm, forward, discfactor, strikes_ttm, optiontypes_ttm, vol_backbone_eta in zip(ttms, forwards, discfactors,
                                                                                        strikes_ttms, optiontypes_ttms,
                                                                                        vol_backbone_etas):
        x0, sigma0, qvar0 = simulate_logsv_x_vol_terminal(ttm=ttm - ttm0,
                                                          x0=x0,
                                                          sigma0=sigma0,
                                                          qvar0=qvar0,
                                                          theta=theta,
                                                          kappa1=kappa1,
                                                          kappa2=kappa2,
                                                          beta=beta,
                                                          volvol=volvol,
                                                          vol_backbone_eta=vol_backbone_eta,
                                                          nb_path=nb_path,
                                                          nb_steps_per_year=nb_steps_per_year,
                                                          is_spot_measure=is_spot_measure)
        ttm0 = ttm
        option_prices, option_std = compute_mc_vars_payoff(x0=x0, sigma0=sigma0, qvar0=qvar0,
                                                           ttm=ttm,
                                                           forward=forward,
                                                           strikes_ttm=strikes_ttm,
                                                           optiontypes_ttm=optiontypes_ttm,
                                                           discfactor=discfactor,
                                                           variable_type=variable_type)
        option_prices_ttm.append(option_prices)
        option_std_ttm.append(option_std)

    return option_prices_ttm, option_std_ttm


@njit(cache=False, fastmath=False)
def simulate_vol_paths(ttm: float,
                       v0: float,
                       theta: float,
                       kappa1: float,
                       kappa2: float,
                       beta: float,
                       volvol: float,
                       is_spot_measure: bool = True,
                       nb_path: int = 100000,
                       nb_steps_per_year: int = 360,
                       brownians: np.ndarray = None,
                       **kwargs
                       ) -> Tuple[np.ndarray, np.ndarray]:
    """
    simulate vol paths on grid_t = [0.0, ttm]
    """
    sigma0 = v0 * np.ones(nb_path)

    nb_steps, dt, grid_t = set_time_grid(ttm=ttm, nb_steps_per_year=nb_steps_per_year)

    if brownians is None:
        brownians = np.sqrt(dt) * np.random.normal(0, 1, size=(nb_steps, nb_path))
    assert brownians.shape == (nb_steps, nb_path)

    if is_spot_measure:
        alpha, adj = -1.0, 0.0
    else:
        alpha, adj = 1.0, beta

    vartheta2 = beta * beta + volvol * volvol
    vartheta = np.sqrt(vartheta2)
    vol_var = np.log(sigma0)
    sigma_t = np.zeros((nb_steps + 1, nb_path))  # sigma grid will increase to include the sigma_0 at t0 = 0
    sigma_t[0, :] = sigma0  # keep first value
    for t_, w1_ in enumerate(brownians):
        vol_var = vol_var + ((kappa1 * theta / sigma0 - kappa1) + kappa2 * (
                theta - sigma0) + adj * sigma0 - 0.5 * vartheta2) * dt + vartheta * w1_
        sigma0 = np.exp(vol_var)
        sigma_t[t_ + 1, :] = sigma0

    return sigma_t, grid_t


def simulate_roughvol_paths(ttm: float,
                            v0: float,
                            theta: float,
                            kappa1: float,
                            kappa2: float,
                            beta: float,
                            volvol: float,
                            H: float,
                            is_spot_measure: bool = True,
                            nb_path: int = 100000,
                            nb_steps_per_year: int = 360,
                            Z: np.ndarray = None,
                            **kwargs
                            ) -> Tuple[np.ndarray, np.ndarray]:
    alpha = H - 0.5
    sigma0 = v0 * np.ones(nb_path)
    total_vol = np.sqrt(beta ** 2 + volvol ** 2)

    nb_steps, dt, grid_t = set_time_grid(ttm=ttm, nb_steps_per_year=nb_steps_per_year)
    cov_matrix = np.zeros((2, 2))
    cov_matrix[0, 0] = dt
    cov_matrix[1, 0] = np.power(dt, alpha + 1.0) / (alpha + 1.0)
    cov_matrix[0, 1] = np.power(dt, alpha + 1.0) / (alpha + 1.0)
    cov_matrix[1, 1] = np.power(dt, 2.0 * alpha + 1.0) / (2.0 * alpha + 1.0)
    A = np.linalg.cholesky(cov_matrix)

    K = lambda t: np.power(t, alpha)
    c, gamm = approximat_kernel(K, epsilon=1e-2, n=nb_steps, delta=dt, T=ttm)
    m = c.size

    kappa = 1  # how many timesteps we move away from the discontinuity of the kernel at t=0
    assert kappa == 1
    assert Z.shape == (kappa + 1, nb_steps * nb_path)
    dWs = np.reshape(A.dot(Z), (kappa + 1, nb_steps, nb_path))

    coeff1 = c * np.exp(-gamm * kappa * dt)
    coeff2 = 1.0 / (1.0 + gamm * dt)
    x0 = np.ones((nb_path,)) * sigma0

    U = np.zeros((m, nb_path))
    drift_factor = cov_matrix[1, 0]
    X_out = np.ones((nb_steps + 1, nb_path)) * np.nan

    X_out[0, :] = x0

    for i in range(nb_steps):
        if i == 0:
            drft_prev = (kappa1 + kappa2 * x0) * (theta - x0)
            vol_prev = total_vol * x0
            Xi = x0 + drft_prev * drift_factor + vol_prev * dWs[1, i, :]
            Xi = pos(Xi)
        else:
            drft_term = (kappa1 + kappa2 * Xi) * (theta - Xi)
            vol_term = total_vol * Xi
            U = coeff2[:, None] * (U + drft_prev * dt + vol_prev * dWs[0, i - kappa, :])
            Xi = x0 + np.sum(coeff1[:, None] * U, axis=0) + drft_term * drift_factor + vol_term * dWs[1, i, :]
            Xi = pos(Xi)

            drft_prev = drft_term
            vol_prev = vol_term
        X_out[i + 1, :] = Xi

    return X_out, grid_t


# @njit(cache=False, fastmath=False)
def simulate_logsv_x_vol_terminal(ttm: float,
                                  x0: np.ndarray,
                                  sigma0: np.ndarray,
                                  qvar0: np.ndarray,
                                  theta: float,
                                  kappa1: float,
                                  kappa2: float,
                                  beta: float,
                                  volvol: float,
                                  vol_backbone_eta: float = 1.0,
                                  is_spot_measure: bool = True,
                                  nb_path: int = 100000,
                                  nb_steps_per_year: int = 360,
                                  W0: Optional[np.ndarray] = None,
                                  W1: Optional[np.ndarray] = None,
                                  dt: Optional[float] = None
                                  ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    mc simulator for terminal values of log-return, vol sigma0, and qvar for log sv model
    """
    if x0.shape[0] == 1:  # initial value
        x0 = x0 * np.zeros(nb_path)
    else:
        assert x0.shape[0] == nb_path

    if qvar0.shape[0] == 1:  # initial value
        qvar0 = np.zeros(nb_path)
    else:
        assert qvar0.shape[0] == nb_path

    if sigma0.shape[0] == 1:
        sigma0 = sigma0 * np.ones(nb_path)
    else:
        assert sigma0.shape[0] == nb_path
    if W0 is None and W1 is None:
        nb_steps1, dt, grid_t = set_time_grid(ttm=ttm, nb_steps_per_year=nb_steps_per_year)
        print(f"nb_steps1={nb_steps1}, dt={dt}")
        sdt = np.sqrt(dt)
        W0_ = sdt * np.random.normal(0, 1, size=(nb_steps1, nb_path))
        W1_ = sdt * np.random.normal(0, 1, size=(nb_steps1, nb_path))
    else:
        sdt = np.sqrt(dt)
        W0_ = sdt * W0
        W1_ = sdt * W1

    if is_spot_measure:
        alpha, adj = -1.0, 0.0
    else:
        alpha, adj = 1.0, beta * vol_backbone_eta  # ? vol_backbone_eta

    # # debug
    # sigma_all = []
    # sigma_all.append(sigma0.copy())
    # # debug

    vartheta2 = beta * beta + volvol * volvol
    vol_backbone_eta2 = vol_backbone_eta * vol_backbone_eta
    vol_var = np.log(sigma0)
    for t_, (w0, w1) in enumerate(zip(W0_, W1_)):
        sigma0_2dt = vol_backbone_eta2 * sigma0 * sigma0 * dt
        x0 = x0 + alpha * 0.5 * sigma0_2dt + vol_backbone_eta * sigma0 * w0
        vol_var = vol_var + ((kappa1 * theta / sigma0 - kappa1) + kappa2 * (
                theta - sigma0) + adj * sigma0 - 0.5 * vartheta2) * dt + beta * w0 + volvol * w1
        sigma0 = np.exp(vol_var)
        qvar0 = qvar0 + 0.5 * (sigma0_2dt + vol_backbone_eta2 * sigma0 * sigma0 * dt)

        # # debug
        # sigma_all.append(sigma0.copy())
        # # debug

    return x0, sigma0, qvar0
    # return None, np.array(sigma_all), None


# @njit(cache=False, fastmath=False)
def simulate_rough_logsv_x_vol_terminal(ttm: float,
                                        x0: np.ndarray,
                                        sigma0: np.ndarray,
                                        qvar0: np.ndarray,
                                        theta: float,
                                        kappa1: float,
                                        kappa2: float,
                                        beta: float,
                                        volvol: float,
                                        H: float,
                                        vol_backbone_eta: float = 1.0,
                                        is_spot_measure: bool = True,
                                        nb_path: int = 100000,
                                        nb_steps_per_year: int = 360,
                                        W0: Optional[np.ndarray] = None,
                                        W1: Optional[np.ndarray] = None,
                                        dt: Optional[float] = None
                                        ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    mc simulator for terminal values of log-return, vol sigma0, and qvar for log sv model
    """
    assert vol_backbone_eta == 1.0

    if x0.shape[0] == 1:  # initial value
        x0 = x0 * np.zeros(nb_path)
    else:
        assert x0.shape[0] == nb_path

    if qvar0.shape[0] == 1:  # initial value
        qvar0 = np.zeros(nb_path)
    else:
        assert qvar0.shape[0] == nb_path

    if sigma0.shape[0] == 1:
        sigma0 = sigma0 * np.ones(nb_path)
    else:
        assert sigma0.shape[0] == nb_path

    total_vol = np.sqrt(beta ** 2 + volvol ** 2)
    nb_steps1, dt, grid_t = set_time_grid(ttm=ttm, nb_steps_per_year=nb_steps_per_year)
    if W0 is None and W1 is None:
        print(f"nb_steps1={nb_steps1}, dt={dt}")
        W0 = np.random.normal(0, 1, size=(nb_steps1, nb_path))
        W1 = np.random.normal(0, 1, size=(2, nb_steps1, nb_path))

    assert W0.shape == (nb_steps1, nb_path)
    assert W1.shape == (2, nb_steps1, nb_path)

    assert is_spot_measure
    alpha = H - 0.5

    assert -0.5 < alpha <= 0.5
    if alpha > 0.0:
        raise NotImplementedError("Beylkin method does not work for increasing kernel")

    cov_matrix = np.zeros((2, 2))
    cov_matrix[0, 0] = dt
    cov_matrix[1, 0] = np.power(dt, alpha + 1.0) / (alpha + 1.0)
    cov_matrix[0, 1] = np.power(dt, alpha + 1.0) / (alpha + 1.0)
    cov_matrix[1, 1] = np.power(dt, 2.0 * alpha + 1.0) / (2.0 * alpha + 1.0)
    A = np.linalg.cholesky(cov_matrix)

    K = lambda t: np.power(t, alpha)
    nb_steps = nb_steps1
    c, gamm = approximat_kernel(K, epsilon=1e-2, n=nb_steps, delta=dt, T=ttm)
    m = c.size

    dt_kappa = 1
    assert dt_kappa == 1

    # matrix of shape (dt_kappa+1, nb_times, nb_path)
    dWs = np.reshape(A.dot(W1.reshape(dt_kappa + 1, nb_steps * nb_path)),
                     (dt_kappa + 1, nb_steps, nb_path))
    dW0s = np.sqrt(dt) * W0

    coeff1 = c * np.exp(-gamm * dt_kappa * dt)
    coeff2 = 1.0 / (1.0 + gamm * dt)

    U = np.zeros((m, nb_path))
    drift_factor = cov_matrix[1, 0]

    # # debug
    # sigma_out = np.ones((nb_steps + 1, nb_path)) * np.nan
    # sigma_out[0, :] = sigma0
    # # debug

    x_i = x0.copy()
    sigma_i = sigma0.copy()
    qvar_i = qvar0.copy()
    for i in range(nb_steps):
        sigma2dt = vol_backbone_eta * vol_backbone_eta * sigma_i * sigma_i * dt
        x_i = x_i + 0.5 * sigma2dt + vol_backbone_eta * sigma_i * dW0s[i, :]
        if i == 0:
            drft_prev = (kappa1 + kappa2 * sigma0) * (theta - sigma0)
            vol_prev = total_vol * sigma0
            sigma_i = sigma0 + drft_prev * drift_factor + vol_prev * dWs[1, i, :]
            sigma_i = pos(sigma_i)
        else:
            drft_term = (kappa1 + kappa2 * sigma_i) * (theta - sigma_i)
            vol_term = total_vol * sigma_i
            U = coeff2[:, None] * (U + drft_prev * dt + vol_prev * dWs[0, i - dt_kappa, :])
            sigma_i = sigma0 + np.sum(coeff1[:, None] * U, axis=0) + drft_term * drift_factor + vol_term * dWs[1, i, :]
            sigma_i = pos(sigma_i)

            drft_prev = drft_term
            vol_prev = vol_term
        qvar_i = qvar_i + 0.5 * (sigma2dt + vol_backbone_eta * vol_backbone_eta * sigma_i * sigma_i * dt)
        # # debug
        # sigma_out[i + 1, :] = sigma_i
        # # debug

    return x_i, sigma_i, qvar_i


def get_randoms_for_chain_valuation(ttms: np.ndarray,
                                    nb_path: int = 100000,
                                    nb_steps_per_year: int = 360,
                                    seed: int = 10
                                    ) -> Tuple[List[np.ndarray], List[np.ndarray], List[np.ndarray], List[int]]:
    """
    we need to fix random normals for suesequent evaluation using mc slices
    outputs as numpy lists
    """
    #
    set_seed(seed)
    W0s = List()
    W1s = List()
    dts = List()
    nb_steps = List()
    ttm0 = 0.0
    for ttm in ttms:
        # qqq
        nb_steps_, dt, grid_t = set_time_grid(ttm=ttm - ttm0, nb_steps_per_year=nb_steps_per_year)
        W0s.append(np.random.normal(0, 1, size=(nb_steps_, nb_path)))
        W1s.append(np.random.normal(0, 1, size=(nb_steps_, nb_path)))
        dts.append(dt)
        nb_steps.append(nb_steps_)
        ttm0 = grid_t[-1]
    return W0s, W1s, dts, nb_steps


def get_randoms_roughvol(ttms: np.ndarray,
                         nb_path: int = 100000,
                         nb_steps_per_year: int = 360,
                         seed: int = 10):
    set_seed(seed)
    nb_steps = List()
    dts = List()
    ttm0 = 0.0
    for ttm in ttms:
        nb_steps_, dt, grid_t = set_time_grid(ttm=ttm - ttm0, nb_steps_per_year=nb_steps_per_year)
        ttm0 = grid_t[-1]
        nb_steps.append(nb_steps_)
        dts.append(dt)

    W0 = np.random.normal(0, 1, (np.sum(nb_steps), nb_path))
    W1 = np.random.normal(0, 1, (2, np.sum(nb_steps), nb_path))
    return W0, W1, dts, nb_steps


# @njit(cache=False, fastmath=True)
def logsv_mc_chain_pricer_fixed_randoms(ttms: np.ndarray,
                                        forwards: np.ndarray,
                                        discfactors: np.ndarray,
                                        strikes_ttms: Tuple[np.ndarray, ...],
                                        optiontypes_ttms: Tuple[np.ndarray, ...],
                                        W0s: Tuple[np.ndarray, ...],
                                        W1s: Tuple[np.ndarray, ...],
                                        dts: Tuple[np.ndarray, ...],
                                        v0: float,
                                        theta: float,
                                        kappa1: float,
                                        kappa2: float,
                                        beta: float,
                                        volvol: float,
                                        vol_backbone_etas: np.ndarray,
                                        is_spot_measure: bool = True,
                                        variable_type: VariableType = VariableType.LOG_RETURN
                                        ) -> Tuple[List[np.ndarray], List[np.ndarray]]:
    """
    chain valuation using fixed randoms
    """
    # starting values
    nb_path = W0s[0].shape[1]
    x0 = np.zeros(nb_path)
    qvar0 = np.zeros(nb_path)
    sigma0 = v0 * np.ones(nb_path)
    ttm0 = 0.0

    # outputs as numpy lists
    option_prices_ttm = List()
    option_std_ttm = List()
    for ttm, forward, discfactor, strikes_ttm, optiontypes_ttm, vol_backbone_eta, W0, W1, dt in zip(ttms, forwards,
                                                                                                    discfactors,
                                                                                                    strikes_ttms,
                                                                                                    optiontypes_ttms,
                                                                                                    vol_backbone_etas,
                                                                                                    W0s, W1s, dts):
        x0, sigma0, qvar0 = simulate_logsv_x_vol_terminal(ttm=ttm - ttm0,
                                                          x0=x0,
                                                          sigma0=sigma0,
                                                          qvar0=qvar0,
                                                          theta=theta,
                                                          kappa1=kappa1,
                                                          kappa2=kappa2,
                                                          beta=beta,
                                                          volvol=volvol,
                                                          vol_backbone_eta=vol_backbone_eta,
                                                          nb_path=nb_path,
                                                          dt=dt,
                                                          is_spot_measure=is_spot_measure,
                                                          W0=W0,
                                                          W1=W1)
        ttm0 = ttm
        option_prices, option_std = compute_mc_vars_payoff(x0=x0, sigma0=sigma0, qvar0=qvar0,
                                                           ttm=ttm,
                                                           forward=forward,
                                                           strikes_ttm=strikes_ttm,
                                                           optiontypes_ttm=optiontypes_ttm,
                                                           discfactor=discfactor,
                                                           variable_type=variable_type)
        option_prices_ttm.append(option_prices)
        option_std_ttm.append(option_std)

    return option_prices_ttm, option_std_ttm


def logsv_roughmc_chain_pricer_fixed_randoms(ttms: np.ndarray,
                                             forwards: np.ndarray,
                                             discfactors: np.ndarray,
                                             strikes_ttms: Tuple[np.ndarray, ...],
                                             optiontypes_ttms: Tuple[np.ndarray, ...],
                                             W0: np.ndarray,
                                             W1: np.ndarray,
                                             dts: List[float],
                                             v0: float,
                                             theta: float,
                                             kappa1: float,
                                             kappa2: float,
                                             beta: float,
                                             volvol: float,
                                             H: float,
                                             nb_steps: List[float],
                                             vol_backbone_etas: np.ndarray,
                                             is_spot_measure: bool = True,
                                             variable_type: VariableType = VariableType.LOG_RETURN
                                             ) -> Tuple[List[np.ndarray], List[np.ndarray]]:
    """
    chain valuation using fixed randoms
    """
    # starting values
    nb_path = W0.shape[1]
    x0 = np.zeros(nb_path)
    qvar0 = np.zeros(nb_path)
    sigma0 = v0 * np.ones(nb_path)

    # outputs as numpy lists
    option_prices_ttm = List()
    option_std_ttm = List()

    for (j, (ttm, forward, discfactor, strikes_ttm, optiontypes_ttm,
             vol_backbone_eta, dt, nb_step)) in enumerate(zip(ttms,
                                                              forwards,
                                                              discfactors,
                                                              strikes_ttms,
                                                              optiontypes_ttms,
                                                              vol_backbone_etas,
                                                              dts,
                                                              nb_steps)):
        nb_step_j = np.sum(nb_steps[:j + 1])
        x_t, sigma_t, qvar_t = simulate_rough_logsv_x_vol_terminal(ttm=ttm,
                                                                   x0=x0,
                                                                   sigma0=sigma0,
                                                                   qvar0=qvar0,
                                                                   theta=theta,
                                                                   kappa1=kappa1,
                                                                   kappa2=kappa2,
                                                                   beta=beta,
                                                                   volvol=volvol,
                                                                   H=H,
                                                                   vol_backbone_eta=vol_backbone_eta,
                                                                   nb_path=nb_path,
                                                                   dt=dt,
                                                                   is_spot_measure=is_spot_measure,
                                                                   W0=W0[:nb_step_j, :],
                                                                   W1=W1[:, :nb_step_j, :])

        option_prices, option_std = compute_mc_vars_payoff(x0=x_t, sigma0=sigma_t, qvar0=qvar_t,
                                                           ttm=ttm,
                                                           forward=forward,
                                                           strikes_ttm=strikes_ttm,
                                                           optiontypes_ttm=optiontypes_ttm,
                                                           discfactor=discfactor,
                                                           variable_type=variable_type)
        option_prices_ttm.append(option_prices)
        option_std_ttm.append(option_std)

    return option_prices_ttm, option_std_ttm


class UnitTests(Enum):
    CHAIN_PRICER = 1
    SLICE_PRICER = 2
    CALIBRATOR = 3
    MC_COMPARISION = 4
    MC_COMPARISION_QVAR = 5
    VOL_PATHS = 6
    TERMINAL_VALUES = 7
    MMA_INVERSE_MEASURE_VS_MC = 8
    SIMULATE_ROUGH_VOL_PATHS = 9


def run_unit_test(unit_test: UnitTests):
    import matplotlib.pyplot as plt
    import seaborn as sns
    import stochvolmodels.data.test_option_chain as chains

    if unit_test == UnitTests.CHAIN_PRICER:
        option_chain = get_btc_test_chain_data()
        logsv_pricer = LogSVPricer()
        model_prices = logsv_pricer.price_chain(option_chain=option_chain, params=LOGSV_BTC_PARAMS)
        print(model_prices)
        logsv_pricer.plot_model_ivols_vs_bid_ask(option_chain=option_chain, params=LOGSV_BTC_PARAMS)

    if unit_test == UnitTests.SLICE_PRICER:
        ttm = 1.0
        forward = 1.0
        strikes = np.array([0.9, 1.0, 1.1])
        optiontypes = np.array(['P', 'C', 'C'])

        logsv_pricer = LogSVPricer()
        model_prices, vols = logsv_pricer.price_slice(params=LOGSV_BTC_PARAMS,
                                                      ttm=ttm,
                                                      forward=forward,
                                                      strikes=strikes,
                                                      optiontypes=optiontypes)
        print(model_prices)
        print(vols)

        for strike, optiontype in zip(strikes, optiontypes):
            model_price, vol = logsv_pricer.price_vanilla(params=LOGSV_BTC_PARAMS,
                                                          ttm=ttm,
                                                          forward=forward,
                                                          strike=strike,
                                                          optiontype=optiontype)
            print(f"{model_price}, {vol}")

    elif unit_test == UnitTests.CALIBRATOR:
        option_chain = get_btc_test_chain_data()
        logsv_pricer = LogSVPricer()
        fit_params = logsv_pricer.calibrate_model_params_to_chain(option_chain=option_chain,
                                                                  params0=LOGSV_BTC_PARAMS)
        print(fit_params)
        logsv_pricer.plot_model_ivols_vs_bid_ask(option_chain=option_chain,
                                                 params=fit_params)

    elif unit_test == UnitTests.MC_COMPARISION:
        option_chain = get_btc_test_chain_data()
        logsv_pricer = LogSVPricer()
        logsv_pricer.plot_model_ivols_vs_mc(option_chain=option_chain,
                                            params=LOGSV_BTC_PARAMS)

    elif unit_test == UnitTests.MC_COMPARISION_QVAR:
        from stochvolmodels.pricers.logsv.vol_moments_ode import compute_analytic_qvar
        logsv_pricer = LogSVPricer()
        ttms = {'1m': 1.0 / 12.0, '6m': 0.5}
        option_chain = chains.get_qv_options_test_chain_data()
        option_chain = OptionChain.get_slices_as_chain(option_chain, ids=list(ttms.keys()))
        forwards = np.array(
            [compute_analytic_qvar(params=LOGSV_BTC_PARAMS, ttm=ttm, n_terms=4) for ttm in ttms.values()])
        print(f"QV forwards = {forwards}")

        option_chain.forwards = forwards  # replace forwards to imply BSM vols
        option_chain.strikes_ttms = List(
            forward * strikes_ttm for forward, strikes_ttm in zip(option_chain.forwards, option_chain.strikes_ttms))

        fig = logsv_pricer.plot_model_ivols_vs_mc(option_chain=option_chain,
                                                  params=LOGSV_BTC_PARAMS,
                                                  variable_type=VariableType.Q_VAR)

    elif unit_test == UnitTests.VOL_PATHS:
        logsv_pricer = LogSVPricer()
        nb_path = 10
        sigma_t, grid_t = logsv_pricer.simulate_vol_paths(params=LOGSV_BTC_PARAMS,
                                                          nb_path=nb_path,
                                                          nb_steps=360)

        vol_paths = pd.DataFrame(sigma_t, index=grid_t, columns=[f"{x + 1}" for x in range(nb_path)])
        print(vol_paths)

    elif unit_test == UnitTests.TERMINAL_VALUES:
        logsv_pricer = LogSVPricer()
        params = LOGSV_BTC_PARAMS
        xt, sigmat, qvart = logsv_pricer.simulate_terminal_values(params=params)
        hx = compute_histogram_data(data=xt, x_grid=params.get_x_grid(), name='Log-price')
        hsigmat = compute_histogram_data(data=sigmat, x_grid=params.get_sigma_grid(), name='Sigma')
        hqvar = compute_histogram_data(data=qvart, x_grid=params.get_qvar_grid(), name='Qvar')
        dfs = {'Log-price': hx, 'Sigma': hsigmat, 'Qvar': hqvar}

        with sns.axes_style("darkgrid"):
            fig, axs = plt.subplots(1, 3, figsize=(18, 10), tight_layout=True)
        for idx, (key, df) in enumerate(dfs.items()):
            axs[idx].fill_between(df.index, np.zeros_like(df.to_numpy()), df.to_numpy(),
                                  facecolor='lightblue', step='mid', alpha=0.8, lw=1.0)
            axs[idx].set_title(key)

    elif unit_test == UnitTests.MMA_INVERSE_MEASURE_VS_MC:
        option_chain = get_btc_test_chain_data()
        logsv_pricer = LogSVPricer()
        logsv_pricer.plot_comp_mma_inverse_options_with_mc(option_chain=option_chain,
                                                           params=LOGSV_BTC_PARAMS)

    elif unit_test == UnitTests.SIMULATE_ROUGH_VOL_PATHS:
        params = LOGSV_BTC_PARAMS
        # params = LogSvParams(sigma0=0.5, theta=0.6, kappa1=1.0, kappa2=0.4, beta=-0.5, volvol=0.75)
        params.H = 0.5
        logsv_pricer = LogSVPricer()
        np.random.seed(16)

        ttm = 1.0
        nb_path = 2 ** 6
        nb_steps_per_year = int(np.ceil(360 * ttm))
        nb_steps, dt, grid_t = set_time_grid(ttm=ttm, nb_steps_per_year=nb_steps_per_year)
        Z = np.random.normal(0, 1, (2, nb_steps * nb_path))

        sigma_t, _ = logsv_pricer.simulate_vol_paths(params,
                                                     brownians=Z[0].reshape((nb_steps, nb_path)) * np.sqrt(dt),
                                                     ttm=ttm,
                                                     nb_path=nb_path)

        params.H = 0.45
        rsigma_t, _ = logsv_pricer.simulate_vol_paths(params,
                                                      brownians=Z,
                                                      ttm=ttm,
                                                      nb_path=nb_path)

        with sns.axes_style('darkgrid'):
            fig, ax = plt.subplots(1, 1, figsize=(10, 6), tight_layout=True)

        rvol_paths = pd.DataFrame(np.concatenate((rsigma_t[:, :2], sigma_t[:, :2]), axis=1),
                                  columns=['rvol_1', 'rvol_2', 'cvol_1', 'cvol_2'],
                                  index=grid_t).sort_index()

        sns.lineplot(data=rvol_paths, ax=ax, markers=False, dashes=False)
        ax.set_title(f"Rough-LogSV paths, $H={params.H:.3f}$ vs LogSV ", color='darkblue')
        ax.set_xlabel("Time", fontsize=12)
        ax.set_ylabel('Vol', fontsize=12)

    plt.show()


if __name__ == '__main__':

    unit_test = UnitTests.SIMULATE_ROUGH_VOL_PATHS

    is_run_all_tests = False
    if is_run_all_tests:
        for unit_test in UnitTests:
            run_unit_test(unit_test=unit_test)
    else:
        run_unit_test(unit_test=unit_test)
