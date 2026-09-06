import { API_URL } from '$lib/things/config';
import { query } from '$app/server';
	import type { Video, Coolfunfacts } from '$lib/things/types';
import * as v from 'valibot';
export const thumbnail = query(v.string(), async (url) => {
	let status = 500;

    try {
        // console.log(`${API_URL}/thumbnail/${url}`)
		const response = await fetch(`${API_URL}/thumbnail/${url}`, {
			method: 'GET'
		});

		status = response.status;

		if (!response.ok) {
			return { thumb: '', statuscode: status };
		}

		const contentType = response.headers.get('content-type') ?? 'image/jpeg';
		const bytes = new Uint8Array(await response.arrayBuffer());
		let binary = '';

		for (const byte of bytes) {
			binary += String.fromCharCode(byte);
		}

		return { thumb: `data:${contentType};base64,${btoa(binary)}`, statuscode: status };
	} catch {
		return { thumb: '', statuscode: status };
	}
});


export const getmoredetail = query(v.string(), async (url) => {
	let status = 500;

    try {
        // console.log(`${API_URL}/thumbnail/${url}`)
		const response = await fetch(`${API_URL}/detail/${url}`, {
			method: 'GET'
		});

		status = response.status;

		if (!response.ok) {
            return { detail: null, statuscode: status };
		}
  
		

		return { detail: (await response.json()).data, statuscode: status };
	} catch {
        return { detail: null, statuscode: status };
	}
});
