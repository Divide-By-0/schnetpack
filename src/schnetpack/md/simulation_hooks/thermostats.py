"""
This module contains various thermostat for regulating the temperature of the system during
molecular dynamics simulations. Apart from standard thermostat for convetional simulations,
a series of special thermostat developed for ring polymer molecular dynamics is also provided.
"""
import torch
from typing import Optional, Sequence

from schnetpack import units as spk_units
from schnetpack.md.simulation_hooks.basic_hooks import SimulationHook
from schnetpack.md.simulator import Simulator, System

from schnetpack.md.utils import YSWeights

__all__ = [
    "ThermostatHook",
    "BerendsenThermostat",
    "LangevinThermostat",
    "NHCThermostat",
]


class ThermostatError(Exception):
    """
    Exception for thermostat class.
    """

    pass


class ThermostatHook(SimulationHook):
    ring_polymer = False
    """
    Basic thermostat hook for simulator class. This class is initialized based on the simulator and system
    specifications during the first MD step. Thermostats are applied before and after each MD step.

    Args:
        temperature_bath (float): Temperature of the heat bath in Kelvin.
        time_constant (float): Thermostat time constant in fs.
        nm_transformation (bool): Auxiliary flag which can be used to switch between bead and normal mode representation
                                  in RPMD. (default=False)
    """

    def __init__(self, temperature_bath: float, time_constant: float):
        super(ThermostatHook, self).__init__()
        self.register_buffer("temperature_bath", torch.tensor(temperature_bath))
        # Convert from fs to internal time units
        self.register_buffer(
            "time_constant", torch.tensor(time_constant * spk_units.fs)
        )

        self.register_buffer("_initialized", torch.tensor(False))

    @property
    def initialized(self):
        """
        Auxiliary property for easy access to initialized flag used for restarts
        """
        return self._initialized.item()

    @initialized.setter
    def initialized(self, flag):
        """
        Make sure initialized is set to torch.tensor for storage in state_dict.
        """
        self._initialized = torch.tensor(flag)

    def on_simulation_start(self, simulator: Simulator):
        """
        Routine to initialize the thermostat based on the current state of the simulator. Reads the device to be used.
        In addition, a flag is set so that the thermostat is not reinitialized upon continuation of the MD.

        Main function is the `_init_thermostat` routine, which takes the simulator as input and must be provided for every
        new thermostat.

        Args:
            simulator (schnetpack.simulation_hooks.simulator.Simulator): Main simulator class containing information on
                                                                         the time step, system, etc.
        """
        if not self.initialized:
            self._init_thermostat(simulator)
            self.initialized = True

        # Move everything to proper device
        self.to(simulator.device)
        self.to(simulator.dtype)

    def on_step_begin(self, simulator: Simulator):
        """
        First application of the thermostat before the first half step of the dynamics. Regulates temperature.

        Main function is the `_apply_thermostat` routine, which takes the simulator as input and must be provided for
        every new thermostat.

        Args:
            simulator (schnetpack.simulator.Simulator): Main simulator class containing information on the time step,
                                                        system, etc.
        """
        # Apply thermostat
        self._apply_thermostat(simulator)

    def on_step_end(self, simulator: Simulator):
        """
        Application of the thermostat after the second half step of the dynamics. Regulates temperature.

        Main function is the `_apply_thermostat` routine, which takes the simulator as input and must be provided for
        every new thermostat.

        Args:
            simulator (schnetpack.simulator.Simulator): Main simulator class containing information on the time step,
                                                        system, etc.
        """
        # Apply thermostat
        self._apply_thermostat(simulator)

    def _init_thermostat(self, simulator: Simulator):
        """
        Dummy routine for initializing a thermostat based on the current simulator. Should be implemented for every
        new thermostat. Has access to the information contained in the simulator class, e.g. number of replicas, time
        step, masses of the atoms, etc.

        Args:
            simulator (schnetpack.simulator.Simulator): Main simulator class containing information on the time step,
                                                        system, etc.
        """
        pass

    def _apply_thermostat(self, simulator: Simulator):
        """
        Dummy routine for applying the thermostat to the system. Should use the implemented thermostat to update the
        momenta of the system contained in `simulator.system.momenta`. Is called twice each simulation time step.

        Args:
            simulator (schnetpack.simulator.Simulator): Main simulator class containing information on the time step,
                                                        system, etc.
        """
        raise NotImplementedError


class BerendsenThermostat(ThermostatHook):
    ring_polymer = False
    """
    Berendsen velocity rescaling thermostat, as described in [#berendsen1]_. Simple thermostat for e.g. equilibrating
    the system, does not sample the canonical ensemble.

    Args:
        temperature_bath (float): Temperature of the external heat bath in Kelvin.
        time_constant (float): Thermostat time constant in fs

    References
    ----------
    .. [#berendsen1] Berendsen, Postma, van Gunsteren, DiNola, Haak:
       Molecular dynamics with coupling to an external bath.
       The Journal of Chemical Physics, 81 (8), 3684-3690. 1984.
    """

    def __init__(self, temperature_bath: float, time_constant: float):
        super(BerendsenThermostat, self).__init__(
            temperature_bath=temperature_bath, time_constant=time_constant
        )

    def _apply_thermostat(self, simulator):
        """
        Apply the Berendsen thermostat via rescaling the systems momenta based on the current instantaneous temperature
        and the bath temperature.

        Args:
            simulator (schnetpack.simulator.Simulator): Main simulator class containing information on the time step,
                                                        system, etc.
        """
        scaling = torch.sqrt(
            1.0
            + simulator.integrator.time_step
            / self.time_constant
            * (self.temperature_bath / simulator.system.temperature - 1)
        )
        simulator.system.momenta = (
            simulator.system.expand_atoms(scaling) * simulator.system.momenta
        )


class LangevinThermostat(ThermostatHook):
    ring_polymer = False
    """
    Basic stochastic Langevin thermostat, see e.g. [#langevin_thermostat1]_ for more details.

    Args:
        temperature_bath (float): Temperature of the external heat bath in Kelvin.
        time_constant (float): Thermostat time constant in fs

    References
    ----------
    .. [#langevin_thermostat1] Bussi, Parrinello:
       Accurate sampling using Langevin dynamics.
       Physical Review E, 75(5), 056707. 2007.
    """

    def __init__(self, temperature_bath: float, time_constant: float):
        super(LangevinThermostat, self).__init__(
            temperature_bath=temperature_bath, time_constant=time_constant
        )

        self.register_buffer("thermostat_factor", None)
        self.register_buffer("c1", None)
        self.register_buffer("c2", None)

    def _init_thermostat(self, simulator: Simulator):
        """
        Initialize the Langevin coefficient matrices based on the system and simulator properties.

        Args:
            simulator (schnetpack.simulator.Simulator): Main simulator class containing information on the time step,
                                                        system, etc.
        """
        # Initialize friction coefficients
        gamma = (
            torch.ones(1, device=simulator.device, dtype=simulator.dtype)
            / self.time_constant
        )

        # Initialize coefficient matrices
        c1 = torch.exp(-0.5 * simulator.integrator.time_step * gamma)
        c2 = torch.sqrt(1 - c1 ** 2)

        self.c1 = c1[:, None, None]
        self.c2 = c2[:, None, None]

        # Get mass and temperature factors
        self.thermostat_factor = torch.sqrt(
            simulator.system.masses * spk_units.kB * self.temperature_bath
        )

    def _apply_thermostat(self, simulator: Simulator):
        """
        Apply the stochastic Langevin thermostat to the systems momenta.

        Args:
            simulator (schnetpack.simulator.Simulator): Main simulator class containing information on the time step,
                                                        system, etc.
        """
        # Get current momenta
        momenta = simulator.system.momenta

        # Generate random noise
        thermostat_noise = torch.randn_like(momenta)

        # Apply thermostat
        simulator.system.momenta = (
            self.c1 * momenta + self.thermostat_factor * self.c2 * thermostat_noise
        )


class NHCThermostat(ThermostatHook):
    """
    Nose-Hover chain thermostat, which links the system to a chain of deterministic Nose-Hoover thermostats first
    introduced in [#nhc_thermostat1]_ and described in great detail in [#nhc_thermostat2]_. Advantage of the NHC
    thermostat is, that it does not apply random perturbations to the system and is hence fully deterministic. However,
    this comes at an increased numerical cost compared to e.g. the stochastic thermostats described above.

    Args:
        temperature_bath (float): Temperature of the external heat bath in Kelvin.
        time_constant (float): Thermostat time constant in fs
        chain_length (int): Number of Nose-Hoover thermostats applied in the chain.
        massive (bool): If set to true, an individual thermostat is applied to each degree of freedom in the system.
                        Can e.g. be used for thermostatting (default=False).
        multi_step (int): Number of steps used for integrating the NH equations of motion (default=2)
        integration_order (int): Order of the Yoshida-Suzuki integrator used for propagating the thermostat (default=3).

    References
    ----------
    .. [#nhc_thermostat1] Tobias, Martyna, Klein:
       Molecular dynamics simulations of a protein in the canonical ensemble.
       The Journal of Physical Chemistry, 97(49), 12959-12966. 1993.
    .. [#nhc_thermostat2] Martyna, Tuckerman, Tobias, Klein:
       Explicit reversible integrators for extended systems dynamics.
       Molecular Physics, 87(5), 1117-1157. 1996.
    """

    def __init__(
        self,
        temperature_bath: float,
        time_constant: float,
        chain_length: Optional[int] = 3,
        massive: Optional[bool] = False,
        multi_step: Optional[int] = 1,
        integration_order: Optional[int] = 3,
    ):
        super(NHCThermostat, self).__init__(
            temperature_bath=temperature_bath, time_constant=time_constant
        )

        self.register_buffer("chain_length", torch.tensor(chain_length))
        self.register_buffer("frequency", 1.0 / self.time_constant)
        self.register_buffer("massive", torch.tensor(massive))

        # Cpmpute kBT, since it will be used a lot
        self.register_buffer("kb_temperature", self.temperature_bath * spk_units.kB)

        # Propagation parameters
        self.register_buffer("multi_step", torch.tensor(multi_step))
        self.register_buffer("integration_order", torch.tensor(integration_order))
        self.register_buffer("time_step", None)

        # Find out number of particles (depends on whether massive or not)
        self.register_buffer("degrees_of_freedom", None)
        self.register_buffer("masses", None)

        self.register_buffer("velocities", None)
        self.register_buffer("positions", None)
        self.register_buffer("forces", None)

    def _init_thermostat(self, simulator: Simulator):
        """
        Initialize the thermostat positions, forces, velocities and masses, as well as the number of degrees of freedom
        seen by each chain link.

        Args:
            simulator (schnetpack.simulator.Simulator): Main simulator class containing information on the time step,
                                                        system, etc.
        """
        # Determine integration step via multi step and Yoshida Suzuki weights
        integration_weights = (
            YSWeights()
            .get_weights(self.integration_order.item())
            .to(simulator.device, simulator.dtype)
        )

        self.time_step = (
            simulator.integrator.time_step * integration_weights / self.multi_step
        )

        # Determine shape of tensors and internal degrees of freedom
        n_replicas = simulator.system.n_replicas
        n_molecules = simulator.system.n_molecules
        n_atoms_total = simulator.system.total_n_atoms

        if self.massive:
            state_dimension = (n_replicas, n_atoms_total, 3, self.chain_length)
            self.degrees_of_freedom = torch.ones(
                (n_replicas, n_atoms_total, 3),
                device=simulator.device,
                dtype=simulator.dtype,
            )
        else:
            # TODO: check if tricks could be applied here
            state_dimension = (n_replicas, n_molecules, 1, self.chain_length)
            self.degrees_of_freedom = (3 * simulator.system.n_atoms[None, :, None]).to(
                simulator.dtype
            )

        # Set up masses
        self._init_masses(state_dimension, simulator)

        # Set up internal variables
        self.positions = torch.zeros(
            state_dimension, device=simulator.device, dtype=simulator.dtype
        )
        self.forces = torch.zeros(
            state_dimension, device=simulator.device, dtype=simulator.dtype
        )
        self.velocities = torch.zeros(
            state_dimension, device=simulator.device, dtype=simulator.dtype
        )

    def _init_masses(self, state_dimension: Sequence[int], simulator: Simulator):
        """
        Auxiliary routine for initializing the thermostat masses.

        Args:
            state_dimension (tuple): Size of the thermostat states. This is used to differentiate between the massive
                                     and the standard algorithm
            simulator (schnetpack.simulation_hooks.simulator.Simulator): Main simulator class containing information on the
                                                                 time step, system, etc.
        """
        self.masses = torch.ones(
            state_dimension, device=simulator.device, dtype=simulator.dtype
        )

        # Get masses of innermost thermostat
        self.masses[..., 0] = (
            self.degrees_of_freedom * self.kb_temperature / self.frequency ** 2
        )
        # Set masses of remaining thermostats
        self.masses[..., 1:] = self.kb_temperature / self.frequency ** 2

    def _propagate_thermostat(self, kinetic_energy):
        """
        Propagation step of the NHC thermostat. Please refer to [#nhc_thermostat2]_ for more detail on the algorithm.

        Args:
            kinetic_energy (torch.Tensor): Kinetic energy associated with the innermost NH thermostats.

        Returns:
            torch.Tensor: Scaling factor applied to the system momenta.

        References
        ----------
        .. [#nhc_thermostat2] Martyna, Tuckerman, Tobias, Klein:
           Explicit reversible integrators for extended systems dynamics.
           Molecular Physics, 87(5), 1117-1157. 1996.
        """
        # Compute forces on first thermostat
        self.forces[..., 0] = (
            kinetic_energy - self.degrees_of_freedom * self.kb_temperature
        ) / self.masses[..., 0]

        scaling_factor = 1.0

        for _ in range(self.multi_step):
            for idx_ys in range(self.integration_order):
                time_step = self.time_step[idx_ys]

                # Update velocities of outermost bath
                self.velocities[..., -1] += 0.25 * self.forces[..., -1] * time_step

                # Update the velocities moving through the beads of the chain
                for chain in range(self.chain_length - 2, -1, -1):
                    coeff = torch.exp(
                        -0.125 * time_step * self.velocities[..., chain + 1]
                    )
                    self.velocities[..., chain] = (
                        self.velocities[..., chain] * coeff ** 2
                        + 0.25 * self.forces[..., chain] * coeff * time_step
                    )

                # Accumulate velocity scaling
                scaling_factor *= torch.exp(-0.5 * time_step * self.velocities[..., 0])
                # Update forces of innermost thermostat
                self.forces[..., 0] = (
                    scaling_factor * scaling_factor * kinetic_energy
                    - self.degrees_of_freedom * self.kb_temperature
                ) / self.masses[..., 0]

                # Update thermostat positions
                # TODO: Only required if one is interested in the conserved
                #  quanity of the NHC.
                self.positions += 0.5 * self.velocities * time_step

                # Update the thermostat velocities
                for chain in range(self.chain_length - 1):
                    coeff = torch.exp(
                        -0.125 * time_step * self.velocities[..., chain + 1]
                    )
                    self.velocities[..., chain] = (
                        self.velocities[..., chain] * coeff ** 2
                        + 0.25 * self.forces[..., chain] * coeff * time_step
                    )
                    self.forces[..., chain + 1] = (
                        self.masses[..., chain] * self.velocities[..., chain] ** 2
                        - self.kb_temperature
                    ) / self.masses[..., chain + 1]

                # Update velocities of outermost thermostat
                self.velocities[..., -1] += 0.25 * self.forces[..., -1] * time_step

        return scaling_factor

    def _compute_kinetic_energy(self, system: System):
        """
        Routine for computing the kinetic energy of the innermost NH thermostats based on the momenta and masses of the
        simulated systems.

        Args:
            system (schnetpack.md.System): System object.

        Returns:
            torch.Tensor: Kinetic energy associated with the innermost NH thermostats. These are summed over the
                          corresponding degrees of freedom, depending on whether a massive NHC is used.

        """
        if self.massive:
            # Compute the kinetic energy (factor of 1/2 can be removed, as it
            # cancels with a times 2)
            kinetic_energy = system.momenta ** 2 / system.masses
            return kinetic_energy
        else:
            return 2.0 * system.kinetic_energy

    def _apply_thermostat(self, simulator: Simulator):
        """
        Propagate the NHC thermostat, compute the corresponding scaling factor and apply it to the momenta of the
        system. If a normal mode transformer is provided, this is done in the normal model representation of the ring
        polymer.

        Args:
            simulator (schnetpack.simulation_hooks.simulator.Simulator): Main simulator class containing information on the
                                                                 time step, system, etc.
        """
        # Get kinetic energy (either for massive or normal degrees of freedom)
        kinetic_energy = self._compute_kinetic_energy(simulator.system)

        # Accumulate scaling factor
        scaling_factor = self._propagate_thermostat(kinetic_energy)

        # Update system momenta
        if not self.massive:
            scaling_factor = simulator.system.expand_atoms(scaling_factor)

        simulator.system.momenta = simulator.system.momenta * scaling_factor

        # self.compute_conserved(simulator.system)

    # TODO: check with logger
    def compute_conserved(self, system):
        conserved = (
            system.kinetic_energy[..., None, None]
            + 0.5 * torch.sum(self.velocities ** 2 * self.masses, 4)
            + system.properties["energy"][..., None, None]
            + self.degrees_of_freedom * self.kb_temperature * self.positions[..., 0]
            + self.kb_temperature * torch.sum(self.positions[..., 1:], 4)
        )
        return conserved
