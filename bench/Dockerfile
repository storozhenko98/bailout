FROM rust:1.90-bookworm@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked

FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171
RUN apt-get update && apt-get install -y --no-install-recommends bash curl python3 git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/target/release/bailout /usr/local/bin/bailout
COPY bench/curl /usr/local/bin/curl
RUN chmod 755 /usr/local/bin/curl
USER 1000:1000
WORKDIR /work
ENV BAILOUT_NO_UPDATE=1 BAILOUT_API_URL=http://localhost:80 NO_COLOR=1 HOME=/work/home
ENTRYPOINT ["/usr/local/bin/bailout", "--max-steps", "8"]
