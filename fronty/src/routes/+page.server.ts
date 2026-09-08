import type { Video } from "$lib/things/types";
import { API_URL } from '$lib/things/config';
import type { PageServerLoad } from './$types';
import { durations } from '$lib/things/remote/data.remote';
export const load: PageServerLoad = async ({ fetch }) => {
	let videos = [] as Array<Video>;
	let response;
	try {
		response = await fetch(`${API_URL}/videonames`);
		if (response.status == 200) {
			videos = await response.json();
		}
	} catch {
		return { videos, statuscode: 500 };
	}
	return { videos, statuscode: response.status, durations: durations() };
};


