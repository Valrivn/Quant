"""
Modal-path EVA gate helper.
"""

from typing import Dict, Any, Optional
import numpy as np
from scipy.stats import gaussian_kde


def eva_modal_gate(eva_paths: np.ndarray, seed: Optional[int] = None) -> Dict[str, Any]:
    """
    Computes mode of EVA distribution using KDE and checks if modal EVA > 0 and P(EVA > 0) >= 0.99.
    Returns: {'modal_eva': float, 'p_positive': float, 'seed': int/None, 'passed': bool}
    """
    eva_paths = np.asarray(eva_paths, dtype=float)
    if eva_paths.size == 0:
        return {'modal_eva': 0.0, 'p_positive': 0.0, 'seed': seed, 'passed': False}

    p_positive = float(np.mean(eva_paths > 0.0))
    
    # Compute mode using gaussian_kde
    if np.all(eva_paths == eva_paths[0]):
        modal_eva = float(eva_paths[0])
    else:
        kde = gaussian_kde(eva_paths)
        x_grid = np.linspace(eva_paths.min(), eva_paths.max(), 1000)
        density = kde(x_grid)
        modal_eva = float(x_grid[np.argmax(density)])
        
    passed = bool(modal_eva > 0.0 and p_positive >= 0.99)

    return {
        'modal_eva': modal_eva,
        'p_positive': p_positive,
        'seed': seed,
        'passed': passed
    }
