/** @type {import('next').NextConfig} */

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:6000';

// Kept in step with vercel.json, which only applies on Vercel. Without these,
// `npm run dev` and any self-hosted deployment run without security headers.
const securityHeaders = [
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'Referrer-Policy', value: 'no-referrer' },
  { key: 'Permissions-Policy', value: 'microphone=(), camera=(), geolocation=()' },
];

const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,

  output: process.env.VERCEL ? undefined : 'standalone',

  async headers() {
    return [{ source: '/:path*', headers: securityHeaders }];
  },

  async rewrites() {
    return [
      // The v1 config proxied /api/recorder/* to localhost:5050. That could never
      // work once CMED was hosted: the rewrite runs on the *server*, so it would
      // reach for the server's own localhost rather than the doctor's PC. The
      // browser now talks to the agent directly over WebSocket.
      {
        source: '/api/backend/:path*',
        destination: `${BACKEND_URL}/api/v1/:path*`,
      },
    ];
  },

  images: {
    remotePatterns: [
      { protocol: 'https', hostname: '**.vercel.app' },
    ],
  },
};

module.exports = nextConfig;
