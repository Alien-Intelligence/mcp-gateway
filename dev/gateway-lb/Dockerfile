# Round-robin load balancer in front of the two-replica gateway pair (test fixture only). The
# config is baked into the image rather than bind-mounted: a build context is copied to the
# daemon, so this also works when the daemon is remote (CI runs it as a dind service).
FROM nginx:1.27.3-alpine
COPY nginx.conf /etc/nginx/nginx.conf
