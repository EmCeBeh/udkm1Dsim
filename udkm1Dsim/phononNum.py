#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of the udkm1Dsimpy module.
#
# udkm1Dsimpy is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>.
#
# Copyright (C) 2017 Daniel Schick

"""A :mod:`PhononNum` module """

__all__ = ["PhononNum"]

__docformat__ = "restructuredtext"

from .phonon import Phonon
from .helpers import finderb
import numpy as np
from os import path
from time import time
from scipy.integrate import solve_ivp
from tqdm.notebook import tqdm


class PhononNum(Phonon):
    """PhononNum

    Base class for numerical phonon simulatuons.

    Args:
        S (object): sample to do simulations with
        force_recalc (boolean): force recalculation of results

    Keyword Args:
        only_heat (boolean): true when including only thermal expanison without
            coherent phonon dynamics
        progress_bar (boolean): enable tqdm progress bar

    Attributes:
        S (object): sample to do simulations with
        only_heat (boolean): force recalculation of results
        progress_bar (boolean): enable tqdm progress bar
        ode_options (dict): options for scipy solve_ivp ode solver, see
            <https://docs.scipy.org/doc/scipy/reference/generated/scipy.integrate.solve_ivp.html>

    References:

        .. [5] A. Bojahr, M. Herzog, D. Schick, I. Vrejoiu, & M. Bargheer
           (2012). `Calibrated real-time detection of nonlinearly propagating
           strain waves. Phys. Rev. B, 86(14), 144306.
           <http://www.doi.org/10.1103/PhysRevB.86.144306>`

    """

    def __init__(self, S, force_recalc, **kwargs):
        super().__init__(S, force_recalc, **kwargs)
        self.ode_options = {
            'method': 'RK23',
            'first_step': None,
            'max_step': np.inf,
            'rtol': 1e-3,
            'atol': 1e-6,
            }

    def __str__(self, output=[]):
        """String representation of this class"""

        class_str = 'Numerical Phonon simulation properties:\n\n'
        class_str += super().__str__()

        return class_str

    def get_strain_map(self, delays, temp_map, delta_temp_map):
        """get_strain_map

        Returns a strain profile for the sample structure for given temperature
        profile. The result can be saved using an unique hash of the sample
        and the simulation parameters in order to reuse it.

        """
        filename = 'strain_map_num_' \
                   + self.get_hash(delays, temp_map, delta_temp_map) \
                   + '.npy'
        full_filename = path.abspath(path.join(self.cache_dir, filename))
        if path.exists(full_filename) and not self.force_recalc:
            # found something so load it
            strain_map = np.load(full_filename)
            self.disp_message('_strain_map_ loaded from file:\n\t' + filename)
        else:
            # file does not exist so calculate and save
            strain_map, sticks_sub_systems, velocities = \
                self.calc_strain_map(delays, temp_map, delta_temp_map)
            self.save(full_filename, [strain_map], '_strain_map_num_')
        return strain_map

    def calc_strain_map(self, delays, temp_map, delta_temp_map):
        """calc_strain_map

        Calculates the _strain_map_ of the sample structure for a given
        _temp_map_ and _delta_temp_map_ and _delay_ vector. Further details
        are given in Ref. [5]. The coupled differential equations are solved
        for each oscillator in a linear chain of masses and springs:

       .. math::

            m_i\ddot{x}_i = -k_i(x_i-x_{i-1})-k_{i+1}(x_i-x_{i+1})
            + m_i\gamma_i(\dot{x}_i-\dot{x}_{i-1})
            + F_i^{heat}(t)

        where :math:`x_i(t) = z_{i}(t) - z_i^0` is the shift of each layer.
        :math:`m_i` is the mass and :math:`k_i = m_i\, v_i^2/c_i^2` is the
        spring constant of each layer. Furthermore an empirical damping term
        :math:`F_i^{damp} = \gamma_i(\dot{x}_i-\dot{x}_{i-1})` is introduced
        and the external force (thermal stress) :math:`F_i^{heat}(t)`.
        The thermal stresses are modelled as spacer sticks which are
        calculated from the linear thermal expansion coefficients. The
        equation of motion can be reformulated as:

        .. math::

            m_i\ddot{x}_i = F_i^{spring} + F_i^{damp} + F_i^{heat}(t)

        The numerical solution also allows for non-harmonic inter-atomic
        potentials of up to the order :math:`M`. Accordingly
        :math:`k_i = (k_i^1 \ldots k_i^{M-1})` can be a vector accounting for
        higher orders of the potential which is in the harmonic case purely
        quadratic (:math:`k_i = k_i^1`). The resulting force from the
        displacement of the springs

        .. math::

            F_i^{spring} = -k_i(x_i-x_{i-1})-k_{i+1}(x_i-x_{i+1})

        includes:

        .. math::

        k_i(x_i-x_{i-1}) = \sum_{j=1}^{M-1} k_i^j (x_i-x_{i-1})^j

        """
        t1 = time()

        # initialize
        L = self.S.get_number_of_layers()
        thicknesses = self.S.get_layer_property_vector('_thickness')
        x0 = np.zeros([2*L])  # initial condition for the shift of the layers

        try:
            delays = delays.to('s').magnitude
        except AttributeError:
            pass

        # check temp_maps
        [temp_map, delta_temp_map] = self.check_temp_maps(temp_map, delta_temp_map, delays)

        # calculate the sticks due to heat expansion first for all delay steps
        self.disp_message('Calculating linear thermal expansion ...')
        sticks, sticks_sub_systems = self.calc_sticks_from_temp_map(temp_map, delta_temp_map)

        if self.only_heat:
            # no coherent dynamics so calculate the strain directly
            strain_map = sticks/np.tile(thicknesses, [np.size(sticks, 0), 1])
            velocities = np.zeros_like(strain_map)  # this is quasi-static
        else:
            # include coherent dynamics
            self.disp_message('Calculating coherent dynamics with ODE solver ...')

            L = self.S.get_number_of_layers()
            masses = self.S.get_layer_property_vector('_mass_unit_area')
            thicknesses = self.S.get_layer_property_vector('_thickness')
            spring_consts = self.S.get_layer_property_vector('spring_const')
            damping = self.S.get_layer_property_vector('_phonon_damping')
            force_from_heat = PhononNum.calc_force_from_heat(sticks, spring_consts)

            # apply scipy's ode-solver together
            if self.progress_bar:  # with tqdm progressbar
                pbar = tqdm()
                pbar.set_description('Delay = {:.3f} ps'.format(delays[0]*1e12))
                state = [delays[0], abs(delays[-1]-delays[0])/100]
            else:  # without progressbar
                pbar = None
                state = None

            sol = solve_ivp(
                PhononNum.ode_func,
                [delays[0], delays[-1]],
                x0,
                args=(delays, force_from_heat, damping, spring_consts, masses, L,
                      pbar, state),
                t_eval=delays,
                **self.ode_options)

            if pbar is not None:  # close tqdm progressbar if used
                pbar.close()

            # calculate the strainMap as the second spacial derivative
            # of the layer shift x(t). The result of the ode solver
            # contains x(t) = X(:,1:N) and v(t) = X(:,N+1:end) the
            # positions and velocities of the layers, respectively.
            # temp = np.diff(X[:, 0:L], 0, 2)
            # temp[:, :+1] = 0
            # strain_map = temp/np.tile(thicknesses, np.size(temp, 0), 1)
            # velocities = X[:, L+1:]
            temp = np.diff(sol.y[0:L, :].T, 1, 1)
            strain_map = temp/np.tile(thicknesses[:-1], [np.size(temp, 0), 1])
            velocities = sol.y[L:, :].T
        self.disp_message('Elapsed time for _strain_map_:'
                          ' {:f} s'.format(time()-t1))
        return strain_map, sticks_sub_systems, velocities

    @staticmethod
    def ode_func(t, X, delays, force_from_heat, damping, spring_consts, masses, L,
                 pbar=None, state=None):
        """ode_func

        Provides the according ode function for the ode solver which has to be
        solved. The ode function has the input :math:`t` and :math:`X(t)` and
        calculates the temporal derivative :math:`\dot{X}(t)` where the vector

        .. math::

            X(t) = [x(t) \; \dot{x}(t)] \quad \mbox{and } \quad
            \dot{X}(t) = [\dot{x}(t) \; \ddot{x}(t)] .

        :math:`x(t)` is the actual shift of each layer.

        """
        if pbar is not None:
            # set everything for the tqdm progressbar
            last_t, dt = state
            n = (t - last_t)/dt
            if n >= 1:
                pbar.update(1)
                pbar.set_description('Delay = {:.3f} ps'.format(t*1e12))
                state[0] = t
            elif n < 0:
                state[0] = t

        # start with the actual ode function
        x = X[0:L]
        v = X[L:]

        # the output must be a column vector
        X_prime = np.zeros([2*L])

        # accelerations = derivative of velocities
        X_prime[L:] = (
            PhononNum.calc_force_from_damping(v, damping, masses)
            + PhononNum.calc_force_from_spring(
                np.r_[np.diff(x), 0],
                np.r_[0, np.diff(x)],
                spring_consts)
            + force_from_heat[:, finderb(t, delays)].squeeze()
            )/masses

        # velocities = derivative of positions
        X_prime[0:L] = v

        return X_prime

    @staticmethod
    def calc_force_from_spring(d_X1, d_X2, spring_consts):
        """calc_force_from_spring

        Calculates the force :math:`F_i^{spring}` acting on each mass due to
        the displacement between the left and right site of that mass.
        .. math::

            F_i^{spring} = -k_i(x_i-x_{i-1})-k_{i+1}(x_i-x_{i+1})

        We introduce-higher order inter-atomic potentials by

        .. math::

            k_i(x_i-x_{i-1}) = \sum_{j=1}^{M-1} k_i^j (x_i-x_{i-1})^j

        where :math:`M-1` is the order of the spring constants.

        """
        try:
            spring_order = np.size(spring_consts, 1)
        except IndexError:
            spring_order = 1

        spring_consts = np.reshape(spring_consts, [np.size(spring_consts, 0), spring_order])

        coeff1 = np.vstack((-spring_consts[0:-1, :], np.zeros([1, spring_order])))
        coeff2 = np.vstack((np.zeros([1, spring_order]), -spring_consts[0:-1, :]))

        temp1 = np.zeros([len(d_X1), spring_order])
        temp2 = np.zeros([len(d_X1), spring_order])

        for i in range(spring_order):
            temp1[:, i] = d_X1**(i+1)
            temp2[:, i] = d_X2**(i+1)

        F = np.sum(coeff2*temp2, 1) - np.sum(coeff1*temp1, 1)

        return F

    @staticmethod
    def calc_force_from_heat(sticks, spring_consts):
        """calc_force_from_heat

        Calculates the force acting on each mass due to the heat expansion,
        which is modelled by spacer sticks.

        """
        M, L = np.shape(sticks)
        F = np.zeros([L, M])
        # traverse time
        for i in range(M):
            F[:, i] = -PhononNum.calc_force_from_spring(
                np.hstack((sticks[i, 0:L-1], 0)),
                np.hstack((0, sticks[i, 0:L-1])),
                spring_consts
                )

        return F

    @staticmethod
    def calc_force_from_damping(v, damping, masses):
        """calc_force_from_damping

        Calculates the force acting on each mass in a linear spring due to
        damping (:math:`\gamma_i`) according to the shift velocity difference
        :math:`v_{i}-v_{i-1}` with :math:`v_i(t) = \dot{x}_i(t)`:

        .. math::

            F_i^{damp} = \gamma_i(\dot{x}_i-\dot{x}_{i-1})

        """
        F = masses*damping*np.diff(v, 0)

        return F
