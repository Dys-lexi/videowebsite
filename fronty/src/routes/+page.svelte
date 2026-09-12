<script lang="ts">
	import { getFrontendUrl } from '$lib/things/config';
	import type { Video, Coolfunfacts } from '$lib/things/types';
	import {videodb} from '$lib/things/store';
	let { data } = $props();
			 const storedVideos = liveQuery(() =>
      videodb.video.toArray()
    );

    let likedNames = $derived.by( () => {
      return new Set(
        ($storedVideos ?? []).filter((video) => video.liked).map((video) => video.name)
      )
	});
    let watched = $derived.by( () => {
      return  ($storedVideos ?? []).filter((video) => video.watched?? 0 > 0).sort((a,b)=> (b.watched?? 0)  - (a.watched?? 0)).map((video) => video.name
      )
	});
	 import { liveQuery } from "dexie";
	import { durations,morevideos } from '$lib/things/remote/data.remote';
	import Hover from '$lib/things/followingmouse.svelte';
		async function togglelike(name: string) {
		const existing = await videodb.video.get(name);
	
	await videodb.video.upsert(name,{
		liked: !(existing?.liked ?? false)
	}) 
	}
	let newvideos: Array<Video> = $state([])
	let videos = $derived([...data.videos,...newvideos].sort((a,b) => b.id - a.id));
		import { onMount } from 'svelte';
	  import InfiniteLoading, { type StateChanger } from '$lib/things/infinite';
	let durationvars = $derived(data.durations)
	// let durationvars = durations()
	async function loadmore(stateChanger: StateChanger) {
		const justloaded: {statuscode:number,videos:Array<Video>} = await morevideos(videos.at(-1).id)
		if (justloaded.statuscode == 200){
			if (justloaded.videos.length == 0){
				stateChanger.complete()
				return
			}
			newvideos = [...newvideos,...justloaded.videos]
			stateChanger.loaded();
			return
		}


stateChanger.error();
	
	}
	let FRONTEND_URL = $derived(getFrontendUrl(page.url.origin));
	import './page.css';
	import { page } from '$app/state';
	import { browser } from '$app/environment';
	import { Logo, Star, Eye } from '$lib/things/const.svelte';
	import { getmoredetail } from '$lib/things/remote/data.remote';
	import { includes } from 'valibot';
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

	async function playVideo(path: string) {
		
		playingVideo = path;
		const existing = await videodb.video.get(path);
		await videodb.video.upsert(path,{
		watched: Math.floor(Date.now() / 1000) 
	}) 
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
							 {#await durationvars} <div class="duration hideme">{  "--:--"} </div>{:then duration}
							 
							<div class="duration hideme">{duration?.durations[video.name] ?? "--:--"} </div>
							
							{/await}
						</button>
						<!-- svelte-ignore a11y_consider_explicit_label -->
						<button onclick={() => playVideo(video.name)} class="play-overlay">
							<div class="play-icon hideme"></div>
						</button>
						<button class="copy-button hideme" onclick={(e) => {copyVideoLink(e, video.name)}}>
							{copiedPath === video.name ? '✓' : '📋'}
						</button>
								<button class="eye-button hideme" onclick={() => {togglelike( video.name)}}>
							{@render Eye("none"  ,(watched.length > 0 && watched[0] == video.name)? "rgba(200,200,255,1)" : (watched.includes(video.name) ? "rgba(100,255,100,1)": "rgba(0,0,0,0.7)" ))}
						</button>
								<button class="like-button hideme" onclick={() => {togglelike( video.name)}}>
							{@render Star("none"  ,likedNames.has(video.name)? "yellow": "rgba(0,0,0,0.7)" )}
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


  <InfiniteLoading onInfinite={loadmore} distance = {500} />