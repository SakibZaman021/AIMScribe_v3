"""
AIMScribe AI Backend - MinIO Client
Object storage for audio files with presigned URL support.
"""

import os
import logging
from datetime import timedelta
from typing import Optional, BinaryIO
from functools import lru_cache

from minio import Minio
from minio.error import S3Error

logger = logging.getLogger(__name__)


class MinIOClient:
    """
    MinIO client for audio file storage.

    Features:
    - Presigned upload URLs (for direct client upload)
    - Presigned download URLs
    - File upload/download
    - Bucket management
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        external_endpoint: str = None,
        region: str = "us-east-1"
    ):
        """
        Initialize the object storage client. S3-compatible: MinIO or Cloudflare R2.

        Args:
            endpoint: storage endpoint (host:port for MinIO,
                      <account>.r2.cloudflarestorage.com for R2)
            access_key: access key
            secret_key: secret key
            bucket: default bucket name
            secure: use HTTPS. Always true for R2.
            external_endpoint: endpoint baked into presigned URLs, for clients
                      that cannot reach the internal one. For R2 leave this
                      unset: the endpoint is already publicly reachable, which
                      is the whole reason R2 avoids the problem below.
            region: signing region. R2 requires "auto"; MinIO accepts anything
                      as long as it matches. The region is part of the SigV4
                      signature, so a mismatch fails every presigned request
                      with SignatureDoesNotMatch and nothing more specific.

        On external_endpoint: a presigned URL carries the address it was signed
        for. Pointing it at a LAN address means every in-flight URL breaks the
        moment the machine's DHCP lease changes - which has already cost one
        session here. R2 removes the problem by having one stable public
        hostname for everyone.
        """
        self.endpoint = endpoint
        self.external_endpoint = external_endpoint or endpoint
        self.bucket = bucket
        self.secure = secure
        self.region = region

        # Internal client for operations (uploads, downloads, bucket checks)
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=region
        )

        # External client for presigned URLs only. The region is fixed rather
        # than discovered, because discovery would call an endpoint this process
        # may not be able to reach.
        if external_endpoint and external_endpoint != endpoint:
            self.presign_client = Minio(
                external_endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
                region=region
            )
        else:
            self.presign_client = self.client

        self._ensure_bucket_exists()
        logger.info(
            f"Object storage initialized: {endpoint}/{bucket} "
            f"(external: {self.external_endpoint}, region: {region}, tls: {secure})")
    
    def _ensure_bucket_exists(self):
        """Check bucket exists (for R2/S3, bucket must be pre-created in dashboard)."""
        try:
            if self.client.bucket_exists(self.bucket):
                logger.info(f"Bucket exists: {self.bucket}")
            else:
                # Try to create (works for MinIO, may fail for R2)
                try:
                    self.client.make_bucket(self.bucket)
                    logger.info(f"Created bucket: {self.bucket}")
                except S3Error as create_error:
                    # R2 doesn't allow bucket creation via API - that's OK if bucket exists
                    logger.warning(f"Could not create bucket (may already exist in R2): {create_error}")
        except S3Error as e:
            # Connection error or permission issue
            logger.error(f"Bucket check failed: {e}")
            # Don't raise - let operations fail later with better error messages
    
    def get_presigned_upload_url(
        self,
        object_name: str,
        expires: int = 300
    ) -> str:
        """
        Generate a presigned URL for uploading.

        Args:
            object_name: Object path (e.g., "audio/session123/clip_1.wav")
            expires: URL expiry in seconds (default: 5 minutes)

        Returns:
            Presigned URL for PUT request
        """
        try:
            # Use presign_client for external URLs (signature includes host)
            url = self.presign_client.presigned_put_object(
                self.bucket,
                object_name,
                expires=timedelta(seconds=expires)
            )
            logger.debug(f"Generated presigned upload URL for: {object_name}")
            return url
        except S3Error as e:
            logger.error(f"Failed to generate presigned upload URL: {e}")
            raise
    
    def get_presigned_download_url(
        self,
        object_name: str,
        expires: int = 3600
    ) -> str:
        """
        Generate a presigned URL for downloading.

        Args:
            object_name: Object path
            expires: URL expiry in seconds (default: 1 hour)

        Returns:
            Presigned URL for GET request
        """
        try:
            # Use presign_client for external URLs (signature includes host)
            url = self.presign_client.presigned_get_object(
                self.bucket,
                object_name,
                expires=timedelta(seconds=expires)
            )
            return url
        except S3Error as e:
            logger.error(f"Failed to generate presigned download URL: {e}")
            raise
    
    def upload_file(
        self,
        object_name: str,
        file_path: str,
        content_type: str = "audio/wav"
    ) -> bool:
        """
        Upload a file to MinIO.
        
        Args:
            object_name: Destination object path
            file_path: Local file path
            content_type: MIME type
            
        Returns:
            True if successful
        """
        try:
            self.client.fput_object(
                self.bucket,
                object_name,
                file_path,
                content_type=content_type
            )
            logger.info(f"Uploaded file: {object_name}")
            return True
        except S3Error as e:
            logger.error(f"Failed to upload file: {e}")
            raise
    
    def upload_data(
        self,
        object_name: str,
        data: BinaryIO,
        length: int,
        content_type: str = "audio/wav"
    ) -> bool:
        """
        Upload data stream to MinIO.
        
        Args:
            object_name: Destination object path
            data: File-like object
            length: Data length in bytes
            content_type: MIME type
            
        Returns:
            True if successful
        """
        try:
            self.client.put_object(
                self.bucket,
                object_name,
                data,
                length,
                content_type=content_type
            )
            logger.info(f"Uploaded data: {object_name}")
            return True
        except S3Error as e:
            logger.error(f"Failed to upload data: {e}")
            raise
    
    def download_file(
        self,
        object_name: str,
        file_path: str
    ) -> str:
        """
        Download a file from MinIO.
        
        Args:
            object_name: Source object path
            file_path: Local destination path
            
        Returns:
            Local file path
        """
        try:
            self.client.fget_object(
                self.bucket,
                object_name,
                file_path
            )
            logger.info(f"Downloaded file: {object_name} -> {file_path}")
            return file_path
        except S3Error as e:
            logger.error(f"Failed to download file: {e}")
            raise
    
    def delete_file(self, object_name: str) -> bool:
        """
        Delete a file from MinIO.
        
        Args:
            object_name: Object path to delete
            
        Returns:
            True if successful
        """
        try:
            self.client.remove_object(self.bucket, object_name)
            logger.info(f"Deleted file: {object_name}")
            return True
        except S3Error as e:
            logger.error(f"Failed to delete file: {e}")
            raise
    
    def file_exists(self, object_name: str) -> bool:
        """Check if a file exists in MinIO."""
        try:
            self.client.stat_object(self.bucket, object_name)
            return True
        except S3Error:
            return False
    
    def list_files(self, prefix: str = "") -> list:
        """
        List files in bucket with optional prefix.
        
        Args:
            prefix: Filter by prefix (e.g., "audio/session123/")
            
        Returns:
            List of object names
        """
        try:
            objects = self.client.list_objects(
                self.bucket,
                prefix=prefix,
                recursive=True
            )
            return [obj.object_name for obj in objects]
        except S3Error as e:
            logger.error(f"Failed to list files: {e}")
            raise
    
    def delete_session_files(self, session_id: str) -> int:
        """
        Delete all files for a session.
        
        Args:
            session_id: Session ID
            
        Returns:
            Number of files deleted
        """
        prefix = f"audio/{session_id}/"
        files = self.list_files(prefix)
        
        for file in files:
            self.delete_file(file)
        
        logger.info(f"Deleted {len(files)} files for session {session_id}")
        return len(files)
    
    @staticmethod
    def generate_object_key(session_id: str, clip_number: int) -> str:
        """
        Generate a standardized object key for an audio clip.
        
        Args:
            session_id: Session ID
            clip_number: Clip number
            
        Returns:
            Object key path
        """
        return f"audio/{session_id}/clip_{clip_number}.wav"


# Global instance
_minio_client: Optional[MinIOClient] = None


def get_minio_client() -> MinIOClient:
    """Get or create the MinIO client singleton."""
    global _minio_client

    if _minio_client is None:
        from config import settings

        _minio_client = MinIOClient(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
            external_endpoint=settings.minio_external_endpoint,
            region=settings.minio_region
        )

    return _minio_client
