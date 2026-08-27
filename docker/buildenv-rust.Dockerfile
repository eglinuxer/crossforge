# crossforge build environment + Rust, for PyO3/maturin wheels.
#
# Kept as a separate layer on top of the base image: the Rust toolchain plus
# a second target's std is ~600MB, and only PyO3 wheel builds need it.
#
#   docker build -t crossforge-buildenv:el8 -f docker/buildenv.Dockerfile docker
#   docker build -t crossforge-buildenv:el8-rust -f docker/buildenv-rust.Dockerfile docker
#   crossforge wheel <project> --image crossforge-buildenv:el8-rust
#
# RUSTUP_HOME is baked in so the rustup proxies find the toolchain; CARGO_HOME
# is deliberately NOT set, so cargo defaults to $HOME/.cargo — the writable
# scratch HOME the wheel builder provides (the image dirs are read-only for
# the unprivileged build user).
ARG BASE=crossforge-buildenv:el8
FROM ${BASE}

ENV RUSTUP_HOME=/opt/rustup

RUN dnf install -y curl && dnf clean all \
    && CARGO_HOME=/opt/cargo curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
       | CARGO_HOME=/opt/cargo sh -s -- -y --profile minimal --no-modify-path \
    && CARGO_HOME=/opt/cargo /opt/cargo/bin/rustup target add \
       x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu \
    && ln -sf /opt/cargo/bin/* /usr/local/bin/ \
    && chmod -R a+rX /opt/rustup /opt/cargo
