# Same-edge CostMatrix fix

The official 3.8.3 CostMatrix rejects every connection whose current edge has a complex-turn-restriction flag before it checks for a direct connection between two initial locations on that same edge. This creates unnecessary loops for self-trips and short trips along the edge. The patch lets initial labels reach the existing same-edge check; all other connections retain the complex-restriction guard.

The Dockerfile starts from the original immutable 3.8.3 image, verifies pinned upstream sources by SHA-256, rebuilds the affected matrix object and service, and runs a synthetic regression before producing the runtime image. Compiler tools stay in the build stage. Map builders, runtime configuration, API version and traffic settings remain those of the base image.

The seven-node synthetic map contains a via-way `no_left_turn` restriction. The test verifies that the original engine reproduces the loop, while patched auto/bus matrices return zero for self-trips and the direct distance for forward trips. Reverse one-way trips and journeys subject to the actual turn restriction retain their detours. It also checks equal locations at different source/target indices.

Build locally on an AMD64 Docker host:

```bash
docker build -f engine/Dockerfile -t spoke-valhalla:3.8.3-complex-edge-1 .
```

The manual **Build patched engine** workflow runs the same checks and publishes the image to GHCR. The CLI pins the tested image digest when a release is adopted. This is a Spoke patch, not an upstream Valhalla release; review/rebase it when updating the engine.
