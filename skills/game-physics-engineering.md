# Game Physics: Simulation and Stability Protocol

## 1. Dynamical Integration & Stability
- **Semi-Implicit Euler**: A symplectic first-order integrator that usually gives better long-term energy behavior than explicit Euler in oscillatory systems. It does not guarantee energy conservation or stability; choose the integrator and timestep for the forces, constraints, and accuracy requirements.
- **Protocol**:
    1. Force and Torque accumulation.
    2. Linear/Angular Acceleration derivation ($a = F/m$, $\alpha = T/I$).
    3. Velocity integration.
    4. Positional/Rotational update.
- **Stability Management**: Use a fixed physics timestep for reproducibility, bound catch-up work after long frames, and validate energy drift and convergence under representative loads.

## 🔄 2. Rigid Body Dynamics
- **Rotational Representation**: Exclusive use of **Quaternions** for orientation tracking and interpolation to avoid Gimbal Lock and ensure spherical linear interpolation (SLERP) accuracy.
- **Inertia Tensors**: Dynamic calculation of local-space inertia tensors based on entity geometry and mass distribution.
- **Constraint Solvers**: Implementation of **Sequential Impulses** or **Projected Gauss-Seidel** solvers for handling joints, contacts, and multi-body constraints.

## 🕵️ 3. Collision Detection Architecture
- **Broad-Phase (Spatial Partitioning)**: 
    - Implementation of **Dynamic AABB Trees** (BVH) or **Spatial Hashing** to minimize collision pair tests to $O(N \log N)$ or better.
    - Mandatory use of **Z-Order Curves** for cache-friendly spatial data layout.
- **Narrow-Phase (Precision Tests)**:
    - **GJK (Gilbert-Johnson-Keerthi)**: For intersection and distance tests between convex hulls.
    - **EPA (Expansion Polytope Algorithm)**: For derivation of penetration vectors and contact normals.
- **Continuous Collision Detection (CCD)**: Implementation of swept-volume or ray-casting tests for high-velocity entities to eliminate tunneling artifacts.

## ⚡ 4. Hardware Optimization & Math
- **SIMD (Single Instruction, Multiple Data)**: Implementation of vector/matrix mathematics using **SSE/AVX** or **NEON** intrinsics for 4-way or 8-way parallel execution.
- **Multi-threaded Resolution**: Partitioning the physics world into independent islands for parallel constraint solving on multi-core architectures.
- **GPU Physics**: Offloading massive-scale particle and non-interfering rigid body simulations to Compute Shaders where throughput requirements exceed CPU capabilities.
