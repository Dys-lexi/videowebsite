<script lang="ts">
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();

	let mouseX = $state(-999);
	let mouseY = $state(-999);
	let hoverWidth = $state(0);
	let hoverHeight = $state(0);
	let innerWidth = $state(0);
	let innerHeight = $state(0);
	const hoverOffset = 12;

	function clamp(value: number, min: number, max: number) {
		return Math.min(Math.max(value, min), Math.max(min, max));
	}

	function followMouse(event: MouseEvent) {
		mouseX = event.clientX;
		mouseY = event.clientY;
	}

	let hoverX = $derived(
		clamp(mouseX + hoverOffset, hoverOffset, innerWidth - hoverWidth - hoverOffset)
	);
	let hoverY = $derived(
		clamp(mouseY + hoverOffset, hoverOffset, innerHeight - hoverHeight - hoverOffset)
	);
</script>

<svelte:window onmousemove={followMouse} bind:innerWidth bind:innerHeight />

<div
	class="hoverprofile"
	bind:clientWidth={hoverWidth}
	bind:clientHeight={hoverHeight}
	style:position="fixed"
	style:top="0"
	style:left="0"
	style:transform={`translate3d(${hoverX}px, ${hoverY}px, 0)`}
	style:width="max-content"
	style:max-width={`calc(100vw - ${hoverOffset * 2}px)`}
	style:box-sizing="border-box"
	style:z-index="10"
	style:display={mouseX !== -999 ? 'flex' : 'none'}
	style:visibility={hoverWidth > 0 && hoverHeight > 0 ? 'visible' : 'hidden'}
>
	{@render children()}
</div>
