import type { Video } from "$lib/things/types";
import { API_URL } from '$lib/things/config';
import type { PageServerLoad } from './$types';
import { durations,morevideos } from '$lib/things/remote/data.remote';
export const load: PageServerLoad = async ({ fetch }) => {
	
	return { ...(await morevideos(-1)), durations: durations() };
};


