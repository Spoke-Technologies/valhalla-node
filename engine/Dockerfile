FROM ghcr.io/valhalla/valhalla-scripted@sha256:24ef7955899dececb94e26c6dfb89d64fabfae875f980432694b0261eb6c251b AS build
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    g++ pkg-config patch osmium-tool libboost-dev libprotobuf-dev libzmq3-dev libczmq-dev libsodium-dev \
    libcurl4-openssl-dev libsqlite3-dev libspatialite-dev libluajit-5.1-dev libgeos-dev \
    libtiff-dev liblz4-dev zlib1g-dev libssl-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY engine /build/engine
COPY valhalla.json /build/seed-config.json
RUN python3 engine/build.py && python3 engine/test_complex_edge.py

FROM ghcr.io/valhalla/valhalla-scripted@sha256:24ef7955899dececb94e26c6dfb89d64fabfae875f980432694b0261eb6c251b
LABEL org.opencontainers.image.source="https://github.com/Spoke-Technologies/valhalla-node" \
      org.opencontainers.image.title="Spoke Valhalla 3.8.3 — same-edge matrix fix" \
      tech.spoketech.valhalla.patch="costmatrix-complex-edge-v1"
COPY --from=build /build/valhalla_service-fixed /usr/local/bin/valhalla_service
COPY --from=build /build/libvalhalla-fixed.a /usr/local/lib/libvalhalla.a
