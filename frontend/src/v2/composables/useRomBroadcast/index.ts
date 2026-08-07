// useRomBroadcast — apply other clients' ROM mutations to this one.
//
// The backend emits `roms:updated` / `roms:deleted` carrying ids only, never a
// serialized ROM: `rom_user` is scoped to the requesting user, so a shared
// payload would show everyone the acting user's status and rating. This
// refetches the affected ROMs itself and pipes them through `useRomSync`, so
// each client gets its own per-user state back.
//
// Installed once near the top of the v2 tree, alongside `installScanLifecycle`.
//
// Three things keep this from being a fetch storm:
//   * Events are batched on a debounce window — a bulk status change over a
//     selection arrives as N separate events (N separate requests server-side).
//   * Only ids this client has on screen are fetched; a change to a ROM nobody
//     is looking at costs nothing.
//   * Past a threshold, one gallery invalidation replaces N refetches.
//
// Scans deliberately don't route through here: `useScanLifecycle` already
// coalesces `scan:scanning_rom` into its own debounced gallery refresh.
import { debounce } from "lodash";
import romApi from "@/services/api/rom";
import socket from "@/services/socket";
import storeRoms, { type SimpleRom } from "@/stores/roms";
import { useRomSync } from "@/v2/composables/useRomSync";
import { useSocketEvent } from "@/v2/composables/useSocketEvent";
import storeGalleryRoms from "@/v2/stores/galleryRoms";

interface RomBroadcast {
  ids: number[];
  actor_user_id: number;
  /** Socket id of the connection that made the change, when it had one. */
  actor_client_id: string | null;
}

// Long enough to collect a bulk action's per-ROM events, short enough that a
// single edit feels immediate on the other screen.
const BATCH_MS = 300;

// Above this many affected ROMs, refetching each one costs more than throwing
// the windows away and letting the viewport refill.
const REFETCH_CEILING = 12;

export function installRomBroadcast() {
  const romsStore = storeRoms();
  const galleryRoms = storeGalleryRoms();
  const { applyRomWrite } = useRomSync();

  const pendingUpdates = new Set<number>();
  const pendingDeletes = new Set<number>();

  function invalidateGallery() {
    if (!galleryRoms.onGalleryView) return;
    galleryRoms.invalidateWindows();
    void galleryRoms.fetchInitialMetadata();
  }

  /** Ids worth spending a request on: the ones actually on screen. */
  function onScreen(ids: number[]): number[] {
    return ids.filter(
      (id) => galleryRoms.getRomById(id) || romsStore.currentRom?.id === id,
    );
  }

  const flush = debounce(async () => {
    const deletes = [...pendingDeletes];
    const updates = [...pendingUpdates].filter((id) => !pendingDeletes.has(id));
    pendingDeletes.clear();
    pendingUpdates.clear();

    // A delete shifts every position after it in the sparse window map, so
    // there's no in-place fix — same reason `galleryRoms.remove` invalidates.
    if (deletes.length > 0) {
      invalidateGallery();
      return;
    }

    const targets = onScreen(updates);
    if (targets.length === 0) return;
    if (targets.length > REFETCH_CEILING) {
      invalidateGallery();
      return;
    }

    const results = await Promise.allSettled(
      targets.map((romId) => romApi.getRom({ romId })),
    );
    for (const result of results) {
      if (result.status !== "fulfilled") continue;
      // `applyRomWrite` for the same reason the dialogs use it: a remote edit
      // can move this ROM out of our filters or reorder it.
      applyRomWrite(result.value.data as SimpleRom);
    }
  }, BATCH_MS);

  /** Our own echo: this connection made the change and already applied it.
   *
   * Matched per connection, not per user. Two tabs of one account are two
   * clients that each need the update, and on a single-user instance every
   * client is the same user — so a per-user check would suppress the event
   * everywhere and the broadcast would do nothing at all.
   *
   * A reconnect between the request and the event changes our socket id, so
   * the match fails and we refetch redundantly. Harmless, and rare. */
  function isOwnEcho(payload: RomBroadcast) {
    return (
      Boolean(payload.actor_client_id) && payload.actor_client_id === socket.id
    );
  }

  useSocketEvent<RomBroadcast>("roms:updated", (payload) => {
    if (isOwnEcho(payload)) return;
    for (const id of payload.ids) pendingUpdates.add(id);
    void flush();
  });

  useSocketEvent<RomBroadcast>("roms:deleted", (payload) => {
    if (isOwnEcho(payload)) return;
    for (const id of payload.ids) pendingDeletes.add(id);
    void flush();
  });
}
