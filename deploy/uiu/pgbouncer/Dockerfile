# PgBouncer, built here rather than pulled from a third party: it sits
# between the API and every patient record, so what is in it is ours.
FROM alpine:3.20

RUN apk add --no-cache pgbouncer postgresql16-client tini \
    && mkdir -p /etc/pgbouncer /var/log/pgbouncer /var/run/pgbouncer \
    && chown -R pgbouncer:pgbouncer /etc/pgbouncer /var/log/pgbouncer /var/run/pgbouncer

COPY pgbouncer.ini /etc/pgbouncer/pgbouncer.ini
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER pgbouncer
EXPOSE 6432

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
