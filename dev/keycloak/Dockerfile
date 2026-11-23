# The dev realm is baked into the image rather than bind-mounted: a build context is copied to
# the daemon, so this also works when the daemon is remote (CI runs it as a dind service).
FROM quay.io/keycloak/keycloak:26.8.2
COPY alien-gateway-dev-realm.json /opt/keycloak/data/import/alien-gateway-dev-realm.json
# The userinfo realm (scripts/gen_userinfo_realm.py): access tokens that carry no identity claims,
# for the gateways that read them from the userinfo endpoint. Imported next to the dev realm.
COPY alien-gateway-userinfo-realm.json /opt/keycloak/data/import/alien-gateway-userinfo-realm.json
