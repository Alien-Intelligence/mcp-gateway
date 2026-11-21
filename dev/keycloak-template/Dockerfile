# The deployable realm template (deploy/keycloak), rendered with render.sh and imported at start,
# exactly as the template's README tells a customer to. A test fixture of the stub target: the
# realm checks in tests/e2e/test_keycloak_realm_template.py run against it. Same image as
# dev/keycloak (a unit test keeps the two in step).
FROM quay.io/keycloak/keycloak:26.8.2

ARG REALM_NAME
ARG GATEWAY_PUBLIC_URL

COPY --chmod=755 deploy/keycloak/render.sh /opt/keycloak/realm-template/render.sh
COPY deploy/keycloak/realm-template.json /opt/keycloak/realm-template/realm-template.json
RUN mkdir -p /opt/keycloak/data/import \
    && REALM_NAME="$REALM_NAME" GATEWAY_PUBLIC_URL="$GATEWAY_PUBLIC_URL" \
       /opt/keycloak/realm-template/render.sh /opt/keycloak/realm-template/realm-template.json \
       > /opt/keycloak/data/import/realm.json
