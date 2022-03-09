import numpy as np
from numpy import *
from subprocess import os
from pathlib import Path

import BayesEoR.Params.params as p
from BayesEoR.Utils import mpiprint


def generate_k_cube_in_physical_coordinates(
        nu, nv, neta, ps_box_size_ra_Mpc,
        ps_box_size_dec_Mpc, ps_box_size_para_Mpc):
    """
    Generates rectilinear k-space cubes in units of inverse Mpc.

    Parameters
    ----------
    nu : int
        Number of pixels on a side for the u-axis in the model uv-plane.
    nv : int
        Number of pixels on a side for the v-axis in the model uv-plane.
    neta : int
        Number of Line of Sight (LoS, frequency axis) Fourier modes.
    ps_box_size_ra_Mpc : float
        Right Ascension (RA) axis extent of the cosmological volume in Mpc from
        which the power spectrum is estimated.
    ps_box_size_dec_Mpc : float
        Declination (DEC) axis extent of the cosmological volume in Mpc from
        which the power spectrum is estimated.
    ps_box_size_para_Mpc : float
        LoS extent of the cosmological volume in Mpc from which the power
        spectrum is estimated.

    Returns
    -------
    mod_k_physical : np.ndarray of floats
        Modulus of each 3D k-space voxel, i.e. sqrt(k_x**2 + k_y**2 + kz**2).
    k_x : np.ndarray of floats
        Array of RA axis Fourier modes in inverse Mpc.
    k_y : np.ndarray of floats
        Array of DEC axis Fourier modes in inverse Mpc.
    k_z : np.ndarray of floats
        Array of LoS axis Fourier modes in inverse Mpc.
    x : np.ndarray of ints
        Array of RA axis Fourier mode pixel coordinates.
    y : np.ndarray of ints
        Array of DEC axis Fourier mode pixel coordinates.
    z : np.ndarray of ints
        Array of LoS axis Fourier mode pixel coordinates.

    """
    # Generate k_cube pixel coordinates
    z, y, x = np.mgrid[-(neta//2):(neta//2),
                       -(nv//2):(nv//2)+1,
                       -(nu//2):(nu//2)+1]

    # Setup k-space arrays
    deltakx = 2.*np.pi / ps_box_size_ra_Mpc
    deltaky = 2.*np.pi / ps_box_size_dec_Mpc
    deltakz = 2.*np.pi / ps_box_size_para_Mpc
    k_z = z * deltakz
    k_y = y * deltaky
    k_x = x * deltakx
    mod_k_physical = (k_z**2. + k_y**2. + k_x**2.)**0.5

    return mod_k_physical, k_x, k_y, k_z, x, y, z


def generate_data_and_noise_vector_instrumental(
        sigma, s, nf, nt, uvw_array_meters, bl_redundancy_array,
        random_seed='', rank=0):
    """
    Creates a noise vector (n), with Hermitian structure based upon the
    uv sampling in the instrument model, and adds this noise to the input,
    noiseless visibilities (s) to form the data vector d = s + n.

    Parameters
    ----------
    sigma : float
        Noise amplitude of |n|^2.  The complex amplitude is calculated as
        sigma/sqrt(2).
    s : np.ndarray of complex floats
        Input signal (visibilities).
    nf : int
        Number of frequency channels.
    nt : int
        Number of times.
    uvw_array_meters : np.ndarray of floats
        Instrument model uv-sampling with shape (nbls, 3).
    bl_redundancy_array : np.ndarray of floats
        Number of baselines per redundant baseline group.
    random_seed : int
        Used to seed `np.random` when generating the noise vector.
    rank : int
        MPI rank.

    Returns
    -------
    d : np.ndarray of complex floats
        Data vector of complex signal + noise visibilities.
    complex_noise_hermitian : np.ndarray of complex floats
        Vector of complex noise amplitudes.
    bl_conjugate_pairs_map : dictionary
        Dictionary containing the array index mapping of conjugate baseline
        pairs based on `uvw_array_meters`.

    """
    if sigma == 0.0:
        complex_noise_hermitian = np.zeros(len(s)) + 0.0j
        d = s + complex_noise_hermitian.flatten()
        return d, complex_noise_hermitian.flatten()

    nbls = len(uvw_array_meters)
    ndata = nbls * nt * nf
    if random_seed:
        mpiprint(
            'Using the following random_seed for dataset noise:',
            random_seed,
            rank=rank
        )
        np.random.seed(random_seed)
    real_noise = np.random.normal(0, sigma/2.**0.5, ndata)

    if random_seed:
        np.random.seed(random_seed*123)
    imag_noise = np.random.normal(0, sigma/2.**0.5, ndata)
    complex_noise = real_noise + 1j*imag_noise
    complex_noise = complex_noise * sigma/complex_noise.std()
    complex_noise_hermitian = complex_noise.copy()

    """
    How to create a conjugate baseline map from the instrument model:

    1. Create a map for a single time step that maps the array indices
       of baselines with (u, v) and (-u, -v)
    2. Add noise to (u, v) and conjugate noise to (-u, -v) using the
       map from step 1 per time and frequency (identical map can be used
       at all frequencies).
    """
    bl_conjugate_pairs_dict = {}
    bl_conjugate_pairs_map = {}
    # Only account for uv-redundancy for now so use
    # uvw_array_meters[:,:2] and exclude w-coordinate
    for i, uvw in enumerate(uvw_array_meters[:, :2]):
        if tuple(uvw*-1) in bl_conjugate_pairs_dict.keys():
            key = bl_conjugate_pairs_dict[tuple(uvw*-1)]
            bl_conjugate_pairs_dict[tuple(uvw)] = key
            bl_conjugate_pairs_map[key] = i
        else:
            bl_conjugate_pairs_dict[tuple(uvw)] = i

    for i_t in range(nt):
        time_ind = i_t * nbls * nf
        for i_freq in range(nf):
            freq_ind = i_freq * nbls
            start_ind = time_ind + freq_ind
            for bl_ind in bl_conjugate_pairs_map.keys():
                conj_bl_ind = bl_conjugate_pairs_map[bl_ind]
                complex_noise_hermitian[start_ind+conj_bl_ind] =\
                    complex_noise_hermitian[start_ind+bl_ind].conjugate()
            complex_noise_hermitian[start_ind:start_ind+nbls] /=\
                bl_redundancy_array[:, 0]**0.5

    d = s + complex_noise_hermitian.flatten()

    return d, complex_noise_hermitian.flatten(), bl_conjugate_pairs_map


def mask_k_cubes(k_x, k_y, k_z, mod_k, neta, nq, nuv):
    """
    Creates a mask and masks Fourier and Large Spectral Scale Model (LSSM)
    modes that are unused for estimating the power spectrum.

    Parameters
    ----------
    k_x : np.ndarray of floats
        Array of Right Ascension (RA) axis Fourier modes in inverse Mpc.
    k_y : np.ndarray of floats
        Array of Declination (DEC) axis Fourier modes in inverse Mpc.
    k_z : np.ndarray of floats
        Array of Line of Sight (LoS, frequency axis) axis Fourier modes in
        inverse Mpc.
    mod_k : np.ndarray of floats
        Modulus of each 3D k-space voxel, i.e. sqrt(k_x**2 + k_y**2 + kz**2).
    neta : int
        Number of Line of Sight (LoS, frequency axis) Fourier modes.
    nq : int
        Number of quadratic modes in the Larse Spectral Scale Model (LSSM).
    nuv : int
        Number of model uv-plane points per frequency channel.  Computed as
        `nuv = nu*nv - 1*np.logical_not(p.fit_for_monopole)`.

    Returns
    -------
    k_mask_vo : np.ndarray of bool
        Vis-ordered array which is True for all mod_k array indices used for
        power spectrum estimation and False otherwise.
    mod_k_vo : np.ndarray
        Vis-ordered `mod_k`.  If `nq` > 0, `mod_k_vo` also contains
        `np.inf` values at all indices associated with the LSSM.

    """
    # Do not include high spatial frequency structure in the power
    # spectral data since these terms aren't included in the data model
    nyquist_k_z_mode = k_z[0, 0, 0]
    sub_nyquist_k_z_mode = k_z[-1, 0, 0]
    # NOTE: the k_z=0 term should not necessarily be masked out since it
    # is still required as a quadratic component (and is not currently
    # explicitly added in there) even if it is not used for calculating
    # the power spectrum.
    if nq == 1:
        high_k_z_selector = k_z == nyquist_k_z_mode
    else:
        high_k_z_selector = np.logical_or(
            k_z == nyquist_k_z_mode, k_z == sub_nyquist_k_z_mode
        )
    high_k_z_mask = np.logical_not(high_k_z_selector)

    if p.include_instrumental_effects:
        high_k_z_mask = high_k_z_mask == high_k_z_mask
        high_k_z_selector = high_k_z_selector != high_k_z_selector

    k_perp = (k_x**2. + k_y**2)**0.5
    kperp_mask = k_perp > 0  # Exclude (u, v) = (0, 0)
    kpara_mask = np.abs(k_z) > 0  # Exclude eta = 0

    if nq > 0:
        k_mask = np.logical_and.reduce((high_k_z_mask, kperp_mask, kpara_mask))
    else:
        k_mask = np.logical_and(kperp_mask, kpara_mask)

    # Flattened arrays move first along k_z, then k_x, and lastly k_y
    k_mask_vo = np.moveaxis(k_mask, 0, -1).flatten()
    mod_k_vo = np.moveaxis(mod_k, 0, -1).flatten()

    if not p.fit_for_monopole:
        # Remove the (u, v) = (0, 0) pixel from the mask and mod_k arrays
        k_mask_vo = k_mask_vo.reshape(-1, neta)
        mod_k_vo = mod_k_vo.reshape(-1, neta)
        k_mask_vo = np.delete(k_mask_vo, nuv//2, axis=0).flatten()
        mod_k_vo = np.delete(mod_k_vo, nuv//2, axis=0).flatten()

    if nq > 0:
        # Mask the Large Spectral Scale Model (LSSM) modes.  These
        # modes are only intended to model the spectrum of a ForeGround (FG)
        # component and should not be used to esimate the EoR power spectrum.

        # In the reshaped arrays below:
        # Moving along the zeroth axis moves first along k_x, then k_y
        # Moving along the first axis moves along k_z
        k_mask_vo = k_mask_vo.reshape(-1, neta)
        mod_k_vo = mod_k_vo.reshape(-1, neta)

        # Append a Large Spectral Scale Model (LSSM) mask to prevent
        # LSSM modes from conributing to the power spectrum
        # Flattened arrays move first along k_z, then k_x, and lastly k_y
        lssm_mask = np.zeros((k_mask_vo.shape[0], nq)).astype(bool)
        k_mask_vo = np.hstack((k_mask_vo, lssm_mask)).flatten()
        lssm_infs = np.zeros((mod_k_vo.shape[0], nq)) + np.inf
        mod_k_vo = np.hstack((mod_k_vo, lssm_infs)).flatten()

    return k_mask_vo, mod_k_vo


def generate_k_cube_model_spherical_binning(
        k_mask_vo, mod_k_vo, ps_box_size_para_Mpc):
    """
    Generates a set of spherical k-space bins from which the 1D power spectrum
    is calculated.

    Parameters
    ----------
    k_mask_vo : np.ndarray of bool
        Vis-ordered array which is True for all `mod_k_vo` array indices used
        for power spectrum estimation and False otherwise.
    mod_k_vo : np.ndarray
        Vis-ordered array of |k| = sqrt(k_x**2 + k_y**2 + k_z**2).
    ps_box_size_para_Mpc : float
        LoS extent of the cosmological volume in Mpc from which the power
        spectrum is estimated.

    Returns
    -------
    k_cube_voxels_in_bin : list
        List containing sublists for each spherically averaged k-bin.  Each
        sublist contains the flattened 3D k-space cube index of all |k| that
        fall within a given spherical k-bin.
    modkbins_containing_voxels : list
        Number of |k| that fall within each spherical k-bin.

    """
    modkscaleterm = 1.35
    deltakpara = 2.*np.pi / ps_box_size_para_Mpc
    binsize = deltakpara * 2.0

    numKbins = 50
    modkbins = np.zeros([numKbins, 2])
    modkbins[0, 0] = 0
    modkbins[0, 1] = binsize

    for m1 in range(1, numKbins, 1):
        binsize = binsize * modkscaleterm
        modkbins[m1, 0] = modkbins[m1-1, 1]
        modkbins[m1, 1] = modkscaleterm * modkbins[m1, 0]

    total_elements = 0
    n_bins = 0
    modkbins_containing_voxels = []
    for i_bin in range(numKbins):
        # NOTE: By requiring k_z>0 the constant term in the 1D FFT is now
        # effectively a quadratic mode! If it is to be included
        # explicitly with the quadratic modes, then k_z==0 should be
        # added to the quadratic selector mask
        n_elements = np.sum(
            np.logical_and.reduce((
                mod_k_vo > modkbins[i_bin, 0],
                mod_k_vo <= modkbins[i_bin, 1],
                k_mask_vo
            ))
        )
        if n_elements > 0:
            n_bins += 1
            total_elements += n_elements
            modkbins_containing_voxels.append((modkbins[i_bin], n_elements))

    k_cube_voxels_in_bin = []
    count = 0
    for i_bin in range(len(modkbins_containing_voxels)):
        relevant_voxels = np.where(
            np.logical_and.reduce((
                mod_k_vo > modkbins_containing_voxels[i_bin][0][0],
                mod_k_vo <= modkbins_containing_voxels[i_bin][0][1],
                k_mask_vo
            ))
        )
        count += len(relevant_voxels[0])
        k_cube_voxels_in_bin.append(relevant_voxels)

    return k_cube_voxels_in_bin, modkbins_containing_voxels


def calc_mean_binned_k_vals(
        mod_k_vo, k_cube_voxels_in_bin, save_k_vals=False,
        k_vals_file='k_vals.txt', k_vals_dir='k_vals', rank=0
        ):
    """
    Calculates the mean of all |k| that fall within a k-bin.

    Parameters
    ----------
    mod_k_vo : np.ndarray
        Vis-ordered array of |k| = sqrt(k_x**2 + k_y**2 + k_z**2).
    k_cube_voxels_in_bin : list
        List containing sublists for each k-bin.  Each sublist contains the
        flattened 3D k-space cube index of all |k| that fall within a given
        k-bin.
    save_k_vals : bool
        If `True`, save mean k values to `k_vals_file`.
    k_vals_file : str
        Filename for saved k values.  Defaults to 'k_vals.txt'.
    k_vals_dir : str
        Directory in which to save k values.  Defaults to './k_vals/'.
    rank : int
        MPI rank.

    Returns
    -------
    k_vals : np.ndarray of floats
        Array containing the mean k for each k-bin.

    """
    k_vals = []
    kbin_edges = []
    nsamples = []
    mpiprint('\n---Calculating k-vals---', rank=rank)
    for i_bin in range(len(k_cube_voxels_in_bin)):
        mean_mod_k = mod_k_vo[k_cube_voxels_in_bin[i_bin]].mean()
        min_k = mod_k_vo[k_cube_voxels_in_bin[i_bin]].min()
        kbin_edges.append(min_k)
        nsamples.append(len(k_cube_voxels_in_bin[i_bin][0]))
        k_vals.append(mean_mod_k)
        mpiprint(i_bin, mean_mod_k, rank=rank)
    max_k = mod_k_vo[k_cube_voxels_in_bin[i_bin]].max()
    kbin_edges.append(max_k)

    k_vals_dir = Path(k_vals_dir)
    if save_k_vals and not (k_vals_dir / k_vals_file).exists():
        if not k_vals_dir.exists():
            mpiprint('Directory not found: \n\n' + k_vals_dir + "\n",
                     rank=rank)
            mpiprint('Creating required directory structure..', rank=rank)
            os.makedirs(k_vals_dir.absolute())

        np.savetxt(k_vals_dir / k_vals_file, k_vals)
        np.savetxt(k_vals_dir / k_vals_file.replace('.txt', '_bins.txt'),
                   kbin_edges)
        np.savetxt(k_vals_dir / k_vals_file.replace('.txt', '_nsamples.txt'),
                   nsamples)

    return np.array(k_vals)


def generate_k_cube_model_cylindrical_binning(
        mod_k_masked, k_z_masked, k_y_masked, k_x_masked, n_k_perp_bins,
        ps_box_size_para_Mpc):
    """
    Generates a set of cylindrical k-space bins from which the 2D power
    spectrum is calculated.

    Parameters
    ----------
    mod_k_masked : np.ndarray of floats
        Array of |k| = sqrt(k_x**2 + k_y**2 + k_z**2) containing only modes
        used to estimate the power spectrum.
    k_z_masked : np.ndarray of floats
        Array of Line of Sight (LoS, frequency axis) Fourier modes used to
        estimate the power spectrum.
    k_y_masked : np.ndarray of floats
        Array of DEC axis Fourier modes used to estimate the power spectrum.
    k_x_masked : np.ndarray of floats
        Array of RA axis Fourier modes used to estimate the power spectrum.
    n_k_perp_bins : int
        Number of bins to make along the k-perpendicular axis where
        k_perp = sqrt(k_x**2 + k_y**2).
    ps_box_size_para_Mpc : float
        LoS extent of the cosmological volume in Mpc from which the power
        spectrum is estimated.

    Returns
    -------
    k_cube_voxels_in_bin : list
        List containing sublists for each spherically averaged k-bin.  Each
        sublist contains the flattened 3D k-space cube index of all |k| that
        fall within a given spherical k-bin.
    modkbins_containing_voxels : list
        Number of |k| that fall within each spherical k-bin.
    k_perp_bins : list
        List of k_perp Fourier modes.

    """
    # define mod_k binning
    modkscaleterm = 1.5  # Value used in BEoRfgs and in 21cmFAST binning
    deltakperp = 2.*np.pi / ps_box_size_para_Mpc
    binsize = deltakperp * 2  # Value used in BEoRfgs

    numKbins = 50
    modkbins = np.zeros([numKbins, 2])
    modkbins[0, 0] = 0
    modkbins[0, 1] = binsize

    for m1 in range(1, numKbins, 1):
        binsize = binsize * modkscaleterm
        modkbins[m1, 0] = modkbins[m1-1, 1]
        modkbins[m1, 1] = modkscaleterm * modkbins[m1, 0]

    total_elements = 0
    n_bins = 0
    modkbins_containing_voxels = []
    for i_bin in range(numKbins):
        # NOTE: By requiring k_z>0 the constant term in the 1D FFT is
        # now effectively a quadratic mode! If it is to be included
        # explicitly with the quadratic modes,, then k_z==0 should be
        # added to the quadratic selector mask
        n_elements = np.sum(
            np.logical_and.reduce(
                (mod_k_masked > modkbins[i_bin, 0],
                 mod_k_masked <= modkbins[i_bin, 1],
                 k_z_masked > 0)
                )
            )
        if n_elements > 0:
            n_bins += 1
            total_elements += n_elements
            modkbins_containing_voxels.append((modkbins[i_bin], n_elements))

    # define k_perp binning
    k_perp_3D = (k_x_masked**2. + k_y_masked**2)**0.5
    k_perp_min = k_perp_3D[np.isfinite(k_perp_3D)].min()
    k_perp_max = k_perp_3D[np.isfinite(k_perp_3D)].max()

    def output_k_perp_bins(k_perp_min, k_perp_max, n_k_perp_bins):
        k_perp_bins = np.vstack(
            (np.linspace(k_perp_min*0.99, k_perp_max*1.01, n_k_perp_bins)[:-1],
             np.linspace(k_perp_min*0.99, k_perp_max*1.01, n_k_perp_bins)[1:])
            ).T
        return k_perp_bins

    def return_k_perp_bins_with_voxels(k_perp_bins):
        total_elements = 0
        n_bins = 0
        k_perp_bins_containing_voxels = []
        for i_bin in range(len(k_perp_bins)):
            # NOTE: By requiring k_z>0 the constant term in the 1D FFT
            # is now effectively a quadratic mode! If it is to be
            # included explicitly with the quadratic modes, then k_z==0
            # should be added to the quadratic selector mask
            k_perp_constraint = np.logical_and.reduce(
                (k_perp_3D > k_perp_bins[i_bin][0],
                 k_perp_3D <= k_perp_bins[i_bin][1]))
            n_elements = np.sum(k_perp_constraint)
            if n_elements > 0:
                n_bins += 1
                total_elements += n_elements
                k_perp_bins_containing_voxels.append(k_perp_bins[i_bin])
        # Note: total_elements should be mod_k_masked.size-2*nuv since
        # it doesn't include the linear and quadratic mode channels
        # print(total_elements, mod_k_masked.size)

        return k_perp_bins_containing_voxels

    n_k_perp_bins_array = [n_k_perp_bins
                           for _ in range(len(modkbins_containing_voxels))]

    # Currently using equal bin widths for all k_z but can make the
    # number of bins / the bin width a function of k_z if that turns out
    # to be useful (i.e. use larger bins at larger k_z where there is
    # less power).
    k_perp_bins = [return_k_perp_bins_with_voxels(
        output_k_perp_bins(k_perp_min, k_perp_max, n_k_perp_bins_val))
        for n_k_perp_bins_val in n_k_perp_bins_array]

    k_cube_voxels_in_bin = []
    count = 0
    for i_mod_k_bin in range(len(modkbins_containing_voxels)):
        for j_k_perp_bin in range(len(k_perp_bins[0])):
            # Since calculating the cylindrical power spectrum - bin in
            # k_z rather than mod_k. However maintain the mod_k bin
            # limits for the k_z binning for approximate bin size
            # consistency with previous results when calculating the
            # cylindrical power spectrum with HERA 37.
            k_z_constraint = np.logical_and.reduce(
                (abs(k_z_masked)
                 > modkbins_containing_voxels[i_mod_k_bin][0][0],
                 abs(k_z_masked)
                 <= modkbins_containing_voxels[i_mod_k_bin][0][1],
                 k_z_masked != 0)
                )
            k_perp_constraint = np.logical_and.reduce(
                (k_perp_3D > k_perp_bins[i_mod_k_bin][j_k_perp_bin][0],
                 k_perp_3D <= k_perp_bins[i_mod_k_bin][j_k_perp_bin][1]))
            relevant_voxels = np.where(
                np.logical_and(k_z_constraint, k_perp_constraint))
            count += len(relevant_voxels[0])
            k_cube_voxels_in_bin.append(relevant_voxels)

    return k_cube_voxels_in_bin, modkbins_containing_voxels, k_perp_bins
