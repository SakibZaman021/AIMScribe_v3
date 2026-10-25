# The nightly backup job, the WAL shipper, and point-in-time recovery.
#
# postgres:16-alpine has pg_dump, pg_basebackup and the server itself at the
# right major version - pitr.sh starts a recovered copy here - but no
# openssl. This adds openssl and the timezone database the schedule is read
# in, and nothing else.
FROM postgres:16-alpine

# rclone copies each encrypted backup off the server (failure review A3).
RUN apk add --no-cache openssl tzdata rclone

COPY backup.sh restore.sh pitr.sh wal-fetch.sh /opt/aims/
# wal-fetch.sh is run by PostgreSQL itself, as restore_command, in a recovery.
RUN chmod 755 /opt/aims/*.sh

# Fail the build, not the first night, if any of them is missing.
RUN openssl version && pg_dump --version && pg_basebackup --version && rclone version \
    && command -v su-exec && command -v timeout

ENTRYPOINT ["/bin/sh", "/opt/aims/backup.sh"]
