<script lang="ts">
	import { getFrontendUrl } from '$lib/things/config';
	import type { Video, Coolfunfacts } from '$lib/things/types';
	let { data } = $props();
	import { durations } from '$lib/things/remote/data.remote';
	import Hover from '$lib/things/followingmouse.svelte';
	let videos = $derived(data.videos);
		import { onMount } from 'svelte';

	let durationvars = $derived(data.durations)
	// let durationvars = durations()

	let FRONTEND_URL = $derived(getFrontendUrl(page.url.origin));
	import './page.css';
	import { page } from '$app/state';
	import { browser } from '$app/environment';
	import { Logo } from '$lib/things/const.svelte';
	import { getmoredetail } from '$lib/things/remote/data.remote';
	let datething: Array<[string, Array<Video>]> = $derived.by(() => {
		let groups = {} as Record<string, Array<Video>>;
		for (const video of videos) {
			const date = new Date(video.timetaken * 1000).toDateString();
			(groups[date] ??= []).push(video);
		}
		for (const group of Object.values(groups)) {
			group.sort((a, b) => b.timetaken - a.timetaken);
		}
		return Object.entries(groups).sort(
			([dateA], [dateB]) => new Date(dateB).getTime() - new Date(dateA).getTime()
		);
	});
	let leaveTimer: ReturnType<typeof setTimeout> | undefined;
	let hoveringvideo: string | null = $state(null);
	let playingVideo: string | null = $state(null);
	let videoEl: HTMLVideoElement | undefined = $state();

	let modalVideoUrl = $derived(
		playingVideo
			? `${FRONTEND_URL}/video/${encodeURIComponent(playingVideo).replace(/%2F/g, '/')}`
			: ''
	);

	function playVideo(path: string) {
		playingVideo = path;
	}

	function closeModal() {
		videoEl?.pause();
		playingVideo = null;
	}

	$effect(() => {
		if (playingVideo && videoEl) {
			videoEl.play();
		}
	});

	let copiedPath: string | null = $state(null);

	function copyTextFallback(text: string) {
		const textarea = document.createElement('textarea');
		textarea.value = text;
		textarea.style.position = 'fixed';
		textarea.style.opacity = '0';
		document.body.appendChild(textarea);
		textarea.focus();
		textarea.select();
		document.execCommand('copy');
		document.body.removeChild(textarea);
	}

	async function copyVideoLink(e: MouseEvent, path: string) {
		e.stopPropagation();
		const encodedPath = encodeURIComponent(path).replace(/%2F/g, '/');
		const url = `${FRONTEND_URL}/video/${encodedPath}`;
		try {
			if (navigator.clipboard) {
				await navigator.clipboard.writeText(url);
			} else {
				copyTextFallback(url);
			}
			copiedPath = path;
			setTimeout(() => {
				if (copiedPath === path) copiedPath = null;
			}, 1000);
		} catch (err) {
			console.error('Failed to copy link:', err);
		}
	}
</script>

<svelte:window
	onkeydown={(e) => {
		if (e.key === 'Escape') closeModal();
	}}
/>

<div class="videosholder">
	{#each datething as [date, videos] (date)}
		<div class="dateholder">
			<div class="date-label">{date}</div>

			<div class="videoholder">
				{#each videos as video (video.name)}
					<div
						class="bleh"
						role="presentation"
						onmouseenter={() => {
							if (leaveTimer) clearTimeout(leaveTimer);
							hoveringvideo = video.name;
						}}
						onmouseleave={() => {
							leaveTimer = setTimeout(() => {
								hoveringvideo = null;
							}, 100);
						}}
					>
						<!-- svelte-ignore a11y_consider_explicit_label -->
						<button class="video-card" onclick={() => playVideo(video.name)}>
							<div class="thumbnail-container">
								<!-- {#if browser} -->
								<img
									class="thumbnail"
									src={`${FRONTEND_URL}/thumbnail/${video.name}`}
									alt={`pants I'm broken`}
									loading="lazy"
								/>
								<!-- {:else}
							{@render Logo()}
						{/if} -->
							</div>
							<div class="timestamp">{new Date(video.timetaken * 1000).toLocaleTimeString()}</div>
							<!-- {console.log(video.duration)} -->
							 {#await durationvars} <div class="duration">{  "--:--"} </div>{:then duration}
							 
							<div class="duration">{duration?.durations[video.name] ?? "--:--"} </div>
							
							{/await}
						</button>
						<!-- svelte-ignore a11y_consider_explicit_label -->
						<button onclick={() => playVideo(video.name)} class="play-overlay">
							<div class="play-icon"></div>
						</button>
						<button class="copy-button" onclick={(e) => copyVideoLink(e, video.name)}>
							{copiedPath === video.name ? '✓' : '📋'}
						</button>
						{#if hoveringvideo === video.name}
							<Hover>
								<div class="hoverholder">
									{#await getmoredetail(video.name)}
										Loading details
									{:then { detail }}
										{#if detail}
											{#each detail as event}
												{#if !event.error}
													<div class="killholder">
														<div class={event.localisattacker ? "killer": "victim"}>{event.attackername}</div>
														<img
															class="gun"
															src={`gunimages/${event.attackerweaponname}.png`}
															alt={`${event.attackerweaponname}`}
														/>

														<div class={event.localisattacker ? "victim": "killer"}>{event.victimname}</div>
													</div>
												{:else}
													missing damage info :(
												{/if}
											{/each}
										{:else}
											No details found :(
										{/if}
									{/await}
								</div>
							</Hover>
						{/if}
					</div>
					<!-- {JSON.stringify(video)} -->
				{/each}
			</div>
		</div>
	{/each}
</div>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="modal"
	class:active={playingVideo !== null}
	onclick={(e) => {
		if (e.target === e.currentTarget) closeModal();
	}}
>
	<div class="modal-content">
		<button class="close-modal" onclick={closeModal}>&times;</button>
		{#if playingVideo}
			<!-- svelte-ignore a11y_media_has_caption -->
			<video bind:this={videoEl} src={modalVideoUrl} controls></video>
		{/if}
	</div>
</div>
