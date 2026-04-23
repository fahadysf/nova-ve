import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		port: Number(process.env.NOVA_VE_FRONTEND_PORT || '5173'),
		proxy: {
			...(process.env.NOVA_VE_HTML5_ORIGIN
				? {
						'/html5': {
							target: process.env.NOVA_VE_HTML5_ORIGIN,
							changeOrigin: true,
							ws: true,
						},
				  }
				: {
						'/html5': {
							target: 'http://127.0.0.1:8081',
							changeOrigin: true,
							ws: true,
						},
				  }),
			'/api': {
				target: process.env.NOVA_VE_BACKEND_ORIGIN || 'http://127.0.0.1:8000',
				changeOrigin: true,
			},
			'/VERSION': {
				target: process.env.NOVA_VE_BACKEND_ORIGIN || 'http://127.0.0.1:8000',
				changeOrigin: true,
			},
		},
	},
});
