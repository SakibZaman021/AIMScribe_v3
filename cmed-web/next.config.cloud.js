/** @type {import('next').NextConfig} */

// Cloud Backend URL - CHANGE THIS to your ngrok/Railway URL
const CLOUD_BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || 'https://your-backend.ngrok.io';

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        // Recorder always runs locally (records from microphone)
        source: '/api/recorder/:path*',
        destination: 'http://localhost:5050/:path*',
      },
      {
        // Backend runs in cloud
        source: '/api/backend/:path*',
        destination: `${CLOUD_BACKEND_URL}/api/v1/:path*`,
      },
    ];
  },
  // Allow images from cloud storage
  images: {
    domains: ['localhost', '*.ngrok.io', '*.railway.app'],
  },
};

module.exports = nextConfig;
