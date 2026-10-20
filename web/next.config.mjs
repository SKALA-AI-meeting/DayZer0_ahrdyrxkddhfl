// 화면에서 /api/* 로 부르면 FastAPI(gateway)로 넘긴다. 같은 주소처럼 보여서 CORS 설정이 필요 없다.
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};

export default nextConfig;
