# The dev configuration is baked into the image rather than bind-mounted: a build context is
# copied to the daemon, so this also works when the daemon is remote (CI runs it as a dind service).
FROM oryd/hydra:v26.2.0
COPY hydra.yml /etc/hydra/hydra.yml
