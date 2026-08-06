/**
 * OBJECT TRACKS — one object followed across time, the thing CVAT is built around.
 *
 * Reported: "Where are the real time object detection following stuff than?"
 *
 * Nothing tracked. The estate had per-FRAME assist (`grounding-dino` → bbox, `sam` → polygon): one
 * call, one image, one box. Draw a box on a moving object and it existed on exactly that frame; the
 * next frame was blank and you drew it again. For a 3-minute clip at 25fps that is 4,500 boxes for
 * one object.
 *
 * A TRACK is the fix, and it is a data interpretation rather than a new store: shapes that share a
 * `group` are the same object, each carrying the `t_start` it was drawn at — its KEYFRAME. Between
 * two keyframes the box is INTERPOLATED, so an annotator marks the object where it changes direction
 * and the frames in between are computed. That is the whole reason CVAT is usable on video: the work
 * is proportional to the object's motion, not to the frame count.
 *
 * The schema already carries it (`group_id`, `t_start`, `t_end` in `EMPTY_SCHEMA`), so this needs no
 * migration — it needed someone to read the rows as a track.
 *
 * THIS MODULE IS THE MODEL AND THE MATHS ONLY. No engine handle, no DOM, no time source, so every
 * rule below is checkable without a decoded video or a GPU.
 */

/** One drawn box at one moment — a keyframe of some track. */
export interface Keyframe {
	id: string;
	/** The track this belongs to. Rows sharing it are the same object over time. */
	group: string;
	/** The moment it was drawn at, in seconds. */
	t: number;
	x: number;
	y: number;
	width: number;
	height: number;
	label: string;
}

/** A track: one object, its keyframes in time order. */
export interface Track {
	group: string;
	label: string;
	keyframes: Keyframe[];
	/** First and last keyframe times — the track's LIFETIME. */
	from: number;
	to: number;
}

/** The row shape this reads. Structural, so a test needs no Arrow table. */
export interface TrackRow {
	id: string;
	group: string;
	tStart: number | null;
	x: number | null;
	y: number | null;
	width: number | null;
	height: number | null;
	label: string;
}

/**
 * Read rows as tracks.
 *
 * A row joins a track when it has BOTH a group and a time — a box with no group is a one-off
 * annotation on a single frame (which stays perfectly valid, it just is not a track), and a box
 * with no time cannot be placed on the timeline at all.
 *
 * Keyframes are sorted by time, because interpolation is meaningless otherwise and the rows arrive
 * in whatever order the table holds them.
 */
export function tracksFrom(rows: TrackRow[]): Track[] {
	const byGroup = new Map<string, Keyframe[]>();

	for (const r of rows) {
		if (!r.group || r.tStart == null) continue;
		if (r.x == null || r.y == null || r.width == null || r.height == null) continue;
		const kf: Keyframe = {
			id: r.id,
			group: r.group,
			t: r.tStart,
			x: r.x,
			y: r.y,
			width: r.width,
			height: r.height,
			label: r.label,
		};
		const list = byGroup.get(r.group);
		if (list) list.push(kf);
		else byGroup.set(r.group, [kf]);
	}

	const out: Track[] = [];
	for (const [group, kfs] of byGroup) {
		kfs.sort((a, b) => a.t - b.t);
		const first = kfs[0];
		const last = kfs[kfs.length - 1];
		if (!first || !last) continue;
		out.push({
			group,
			// The label of the FIRST keyframe. A track whose label changes mid-way is a relabelled
			// object, not two objects, and the timeline needs one name for the row.
			label: first.label,
			keyframes: kfs,
			from: first.t,
			to: last.t,
		});
	}
	// Tracks in lifetime order, so the timeline reads top-to-bottom as the clip plays.
	out.sort((a, b) => a.from - b.from || a.group.localeCompare(b.group));
	return out;
}

/** A box with no identity — what a track evaluates to at some moment. */
export interface Box {
	x: number;
	y: number;
	width: number;
	height: number;
}

/**
 * Where the object IS at time `t`.
 *
 * Linear between the two surrounding keyframes. Linear rather than anything smoother on purpose: an
 * annotator corrects by adding a keyframe, and a spline would move the box in places they did not
 * touch, so the correction would not do what it looks like it does. Predictability beats smoothness
 * when the output is training data.
 *
 * OUTSIDE THE LIFETIME IT IS NULL, not clamped to the nearest keyframe. A clamped box would assert
 * the object is present before it enters and after it leaves — inventing labels for frames nobody
 * annotated, which is the worst thing an annotation tool can do. Absence is the honest answer.
 */
export function boxAt(track: Track, t: number): Box | null {
	const kfs = track.keyframes;
	if (kfs.length === 0) return null;
	if (t < track.from || t > track.to) return null;

	// Exactly on, or the single-keyframe case (a track that exists for one instant).
	let before = kfs[0] as Keyframe;
	let after = kfs[kfs.length - 1] as Keyframe;
	for (const kf of kfs) {
		if (kf.t <= t) before = kf;
		if (kf.t >= t) {
			after = kf;
			break;
		}
	}

	if (before.t === after.t) return { x: before.x, y: before.y, width: before.width, height: before.height };

	const span = after.t - before.t;
	const f = (t - before.t) / span;
	return {
		x: before.x + (after.x - before.x) * f,
		y: before.y + (after.y - before.y) * f,
		width: before.width + (after.width - before.width) * f,
		height: before.height + (after.height - before.height) * f,
	};
}

/** Is `t` exactly a keyframe of this track? The timeline marks these, and an annotator editing ON a
 *  keyframe is changing an authored value rather than an interpolated one — a different act. */
export function isKeyframe(track: Track, t: number, tolerance = 1e-6): boolean {
	return track.keyframes.some((k) => Math.abs(k.t - t) <= tolerance);
}

/** Every track that is alive at `t`, with its box — what the canvas draws on the current frame. */
export function boxesAt(tracks: Track[], t: number): { track: Track; box: Box }[] {
	const out: { track: Track; box: Box }[] = [];
	for (const track of tracks) {
		const box = boxAt(track, t);
		if (box) out.push({ track, box });
	}
	return out;
}

/**
 * A fresh track id.
 *
 * Prefixed and random rather than sequential: two annotators working the same item offline would
 * both produce `track-1`, and their tracks would MERGE on save — silently, because sharing a group
 * is exactly what makes two rows one object. A collision here is a data-corruption bug, not a
 * cosmetic one.
 */
export function newTrackId(random: () => number = Math.random): string {
	return `trk-${Math.floor(random() * 0x100000000).toString(36)}`;
}
