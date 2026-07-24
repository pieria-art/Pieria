/* global Module, Log */

/**
 * MMM-Pieria
 * A MagicMirror² module that shows the current curated artwork + museum placard
 * from a running Pieria server (https://github.com/AiwendilInTheWoods/Pieria).
 *
 * It is a thin client over the server's existing display feed: it GETs /next-image,
 * which returns the image URL, the placard metadata, and the display cadence. Each call
 * advances the server's rotation for this display_id, so the module fetches once per
 * cycle on a timer (honoring the server's display_time by default) — never rapid-polls.
 *
 * Front-end only: Pieria serves wide-open CORS, so no node_helper is required.
 */
Module.register("MMM-Pieria", {
	defaults: {
		serverUrl: "http://localhost:8000", // base URL of your Pieria server
		playlist: "", // playlist/collection name; blank = use the server's first playlist
		displayId: "magicmirror", // identifies this display to the server's rotation state
		updateInterval: 0, // ms between artworks; 0 = honor the server's display_time
		minRefresh: 15000, // never refresh faster than this (ms) — protects the server
		retryDelay: 20000, // wait this long (ms) before retrying after a fetch error
		showImage: true,
		showPlacard: true,
		showDescription: true,
		maxDescriptionChars: 0, // 0 = no truncation
		crossfade: true,
		imageMaxHeight: null // e.g. "70vh" or "600px"; null = let CSS/region decide
	},

	start() {
		Log.info(`Starting module: ${this.name}`);
		this.current = null; // last successful /next-image payload
		this.status = "Connecting to Pieria…";
		this.resolvedPlaylist = this.config.playlist || null;
		this.timer = null;
		this.fetchCurrent();
	},

	getStyles() {
		return ["MMM-Pieria.css"];
	},

	// ---- networking -------------------------------------------------------

	// Join the configured server base with a (possibly relative) path.
	absUrl(path) {
		if (!path) return "";
		if (/^https?:\/\//i.test(path)) return path;
		return this.config.serverUrl.replace(/\/+$/, "") + path;
	},

	// Fetch JSON with a sane timeout so a dead server doesn't hang the module.
	async getJson(url, timeoutMs = 12000) {
		const controller = new AbortController();
		const t = setTimeout(() => controller.abort(), timeoutMs);
		try {
			const res = await fetch(url, { signal: controller.signal, cache: "no-store" });
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			return await res.json();
		} finally {
			clearTimeout(t);
		}
	},

	// /next-image requires a playlist name. If the user didn't set one, ask the
	// server for its playlists and use the first — so the module works out of the box.
	async resolvePlaylist() {
		if (this.resolvedPlaylist) return this.resolvedPlaylist;
		const lists = await this.getJson(`${this.config.serverUrl.replace(/\/+$/, "")}/playlists`);
		if (!Array.isArray(lists) || !lists.length || !lists[0].name) {
			throw new Error("no playlists on server");
		}
		this.resolvedPlaylist = lists[0].name;
		Log.info(`${this.name}: defaulting to first playlist "${this.resolvedPlaylist}"`);
		return this.resolvedPlaylist;
	},

	async fetchCurrent() {
		try {
			const playlist = await this.resolvePlaylist();
			const base = this.config.serverUrl.replace(/\/+$/, "");
			const params = new URLSearchParams({
				playlist_name: playlist,
				display_id: this.config.displayId,
				direction: "1"
			});
			const data = await this.getJson(`${base}/next-image?${params.toString()}`);
			this.current = data;
			this.status = null;
			this.updateDom(this.config.crossfade ? 1000 : 0);
			this.scheduleNext(this.nextDelay(data));
		} catch (err) {
			Log.error(`${this.name}: fetch failed — ${err.message}`);
			// Keep showing the last good artwork if we have one; otherwise show status.
			if (!this.current) {
				this.status = `Can't reach Pieria at ${this.config.serverUrl}`;
				this.updateDom(0);
			}
			this.scheduleNext(this.config.retryDelay);
		}
	},

	// How long to wait before the next artwork. Honor the server's per-content
	// display_time unless the user pinned updateInterval; never go below minRefresh.
	nextDelay(data) {
		const fromServer = (data && data.display_time ? data.display_time : 60) * 1000;
		const wanted = this.config.updateInterval > 0 ? this.config.updateInterval : fromServer;
		return Math.max(wanted, this.config.minRefresh);
	},

	scheduleNext(delayMs) {
		if (this.timer) clearTimeout(this.timer);
		this.timer = setTimeout(() => this.fetchCurrent(), delayMs);
	},

	// ---- rendering --------------------------------------------------------

	// Pure metadata -> display strings. Kept side-effect-free so it can be unit-tested
	// without a DOM or the MagicMirror runtime.
	buildPlacard(metadata) {
		const m = metadata || {};
		const bits = [m.date_display || m.creation_date, m.medium].filter(Boolean);
		return {
			title: m.title || "Untitled",
			artist: m.agent_name || "Unknown Artist",
			meta: bits.join("  ·  "),
			description: m.description || ""
		};
	},

	truncate(text, max) {
		if (!max || !text || text.length <= max) return text;
		return text.slice(0, max).replace(/\s+\S*$/, "") + "…";
	},

	getDom() {
		const wrapper = document.createElement("div");
		wrapper.className = "MMM-Pieria";

		if (!this.current) {
			const status = document.createElement("div");
			status.className = "sd-status dimmed light small";
			status.textContent = this.status || "Loading…";
			wrapper.appendChild(status);
			return wrapper;
		}

		const data = this.current;

		if (this.config.showImage && data.image_url) {
			const img = document.createElement("img");
			img.className = "sd-image";
			img.src = this.absUrl(data.image_url);
			img.alt = (data.metadata && data.metadata.title) || "Artwork";
			if (this.config.imageMaxHeight) img.style.maxHeight = this.config.imageMaxHeight;
			wrapper.appendChild(img);
		}

		if (this.config.showPlacard) {
			const p = this.buildPlacard(data.metadata);
			const placard = document.createElement("div");
			placard.className = "sd-placard";

			const title = document.createElement("div");
			title.className = "sd-title bright";
			title.textContent = p.title;
			placard.appendChild(title);

			const artist = document.createElement("div");
			artist.className = "sd-artist";
			artist.textContent = p.artist;
			placard.appendChild(artist);

			if (p.meta) {
				const meta = document.createElement("div");
				meta.className = "sd-meta dimmed small";
				meta.textContent = p.meta;
				placard.appendChild(meta);
			}

			if (this.config.showDescription && p.description) {
				const desc = document.createElement("div");
				desc.className = "sd-description small";
				desc.textContent = this.truncate(p.description, this.config.maxDescriptionChars);
				placard.appendChild(desc);
			}

			wrapper.appendChild(placard);
		}

		return wrapper;
	}
});
