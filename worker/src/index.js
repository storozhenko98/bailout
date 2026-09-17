/** Static landing page plus compatibility routes for older bailout clients. */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith('/v1/') || url.pathname === '/health') {
      url.hostname = 'api.bailout.dev';
      return env.API.fetch(new Request(url, request));
    }
    if (url.pathname === '/install.sh') {
      return new Response(null, { status: 302, headers: {
        Location: 'https://raw.githubusercontent.com/storozhenko98/bailout/main/install.sh',
        'Cache-Control': 'no-store',
      } });
    }
    return env.ASSETS.fetch(request);
  },
};
