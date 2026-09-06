export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:34253";

export function getFrontendUrl(origin: string) {
	return API_URL === "http://localhost:34253" ? "https://clips.xixya.com/api" : `${origin}/api`;
}