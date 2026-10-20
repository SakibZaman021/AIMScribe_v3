# The nightly backup job.
#
# postgres:16-alpine has pg_dump of the right major version but no openssl,
# so encrypted backups (and every restore of one) failed. This adds openssl
# and the timezone database the schedule is read in, and nothing else.
FROM postgres:16-alpine

# rclone copies each encrypted backup off the server (failure review A3).
RUN apk add --no-cache openssl tzdata rclone

COPY backup.sh restore.sh /opt/aims/

# Fail the build, not the first night, if any of them is missing.
RUN openssl version && pg_dump --version && rclone version

ENTRYPOINT ["/bin/sh", "/opt/aims/backup.sh"]
