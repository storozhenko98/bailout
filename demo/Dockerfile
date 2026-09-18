FROM ubuntu:24.04
RUN apt-get update -qq && apt-get install -y --no-install-recommends bash curl ca-certificates tar gzip && rm -rf /var/lib/apt/lists/*
WORKDIR /root
ENV TERM=xterm-256color LANG=C.UTF-8
CMD ["bash", "--noprofile", "--norc", "-i"]
