import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Platform } from "@/stores/platforms";
import storeRoms, { type SimpleRom } from "@/stores/roms";
import storeGalleryRoms from "@/v2/stores/galleryRoms";
import { installRomBroadcast } from "./index";

const { getRom, getRoms, applyRomWrite } = vi.hoisted(() => ({
  getRom: vi.fn(),
  getRoms: vi.fn(),
  applyRomWrite: vi.fn(),
}));

vi.mock("@/services/api/rom", () => ({
  default: { getRom, getRoms },
}));

vi.mock("@/v2/composables/useRomSync", () => ({
  useRomSync: () => ({ syncRom: vi.fn(), applyRomWrite }),
}));

// Capture the handlers instead of touching a real socket.
const handlers = new Map<string, (payload: unknown) => void>();
vi.mock("@/v2/composables/useSocketEvent", () => ({
  useSocketEvent: (event: string, handler: (payload: unknown) => void) => {
    handlers.set(event, handler);
    return { stop: vi.fn() };
  },
}));

const OUR_SOCKET = "socket-self";
vi.mock("@/services/socket", () => ({
  default: {
    get id() {
      return OUR_SOCKET;
    },
  },
}));

// A different connection — another tab of the same account, or another user.
const OTHER_CLIENT = "socket-other";

function makeRom(id: number): SimpleRom {
  return { id, name: `Rom ${id}` } as unknown as SimpleRom;
}

/** Seed the gallery with the given ids so they count as "on screen". */
function seedGallery(ids: number[]) {
  const gallery = storeGalleryRoms();
  gallery.setCurrentPlatform({ id: 1 } as unknown as Platform);
  ids.forEach((id, position) => gallery.byPosition.set(position, makeRom(id)));
  gallery.loadedWindows.add(0);
  gallery.metadataLoaded = true;
  gallery.total = ids.length;
  return gallery;
}

function emitUpdated(
  ids: number[],
  actorClientId: string | null = OTHER_CLIENT,
) {
  handlers.get("roms:updated")?.({
    ids,
    actor_user_id: 1,
    actor_client_id: actorClientId,
  });
}

function emitDeleted(
  ids: number[],
  actorClientId: string | null = OTHER_CLIENT,
) {
  handlers.get("roms:deleted")?.({
    ids,
    actor_user_id: 1,
    actor_client_id: actorClientId,
  });
}

/** Let the debounce fire, then drain the async flush. */
async function settle() {
  await vi.advanceTimersByTimeAsync(400);
}

describe("installRomBroadcast", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setActivePinia(createPinia());
    handlers.clear();
    getRom.mockReset();
    getRoms.mockReset();
    applyRomWrite.mockReset();
    getRoms.mockResolvedValue({
      data: { total: 0, items: [], char_index: {}, rom_id_index: [] },
    });
    getRom.mockImplementation(({ romId }: { romId: number }) =>
      Promise.resolve({ data: makeRom(romId) }),
    );
    storeRoms().currentRom = null;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("refetches a remotely changed ROM and applies it", async () => {
    seedGallery([10, 11]);
    installRomBroadcast();

    emitUpdated([10]);
    await settle();

    expect(getRom).toHaveBeenCalledTimes(1);
    expect(getRom).toHaveBeenCalledWith({ romId: 10 });
    expect(applyRomWrite).toHaveBeenCalledTimes(1);
  });

  it("ignores the echo of a write this connection made", async () => {
    seedGallery([10]);
    installRomBroadcast();

    emitUpdated([10], OUR_SOCKET);
    await settle();

    expect(getRom).not.toHaveBeenCalled();
  });

  // The whole point of matching per connection: a second tab of the same
  // account is a different client and must still update. A per-user check
  // would suppress this, and on a single-user instance it would suppress
  // everything.
  it("applies a write from another tab of the same account", async () => {
    seedGallery([10]);
    installRomBroadcast();

    emitUpdated([10], OTHER_CLIENT);
    await settle();

    expect(getRom).toHaveBeenCalledWith({ romId: 10 });
    expect(applyRomWrite).toHaveBeenCalledTimes(1);
  });

  it("applies a write from a client with no socket id at all", async () => {
    seedGallery([10]);
    installRomBroadcast();

    // An API client or curl: nothing to suppress against.
    emitUpdated([10], null);
    await settle();

    expect(getRom).toHaveBeenCalledWith({ romId: 10 });
  });

  it("batches the burst a bulk action produces into one pass", async () => {
    seedGallery([10, 11, 12]);
    installRomBroadcast();

    // A bulk status change is one request per ROM, so one event per ROM.
    emitUpdated([10]);
    emitUpdated([11]);
    emitUpdated([12]);
    emitUpdated([10]); // duplicate id within the window
    await settle();

    expect(getRom).toHaveBeenCalledTimes(3);
  });

  it("spends no request on a ROM this client isn't showing", async () => {
    seedGallery([10]);
    installRomBroadcast();

    emitUpdated([999]);
    await settle();

    expect(getRom).not.toHaveBeenCalled();
    expect(applyRomWrite).not.toHaveBeenCalled();
  });

  it("still refetches the ROM open in GameDetails when the gallery is elsewhere", async () => {
    seedGallery([10]);
    storeRoms().currentRom = { id: 42 } as never;
    installRomBroadcast();

    emitUpdated([42]);
    await settle();

    expect(getRom).toHaveBeenCalledWith({ romId: 42 });
  });

  it("collapses to a single gallery invalidation past the refetch ceiling", async () => {
    const ids = Array.from({ length: 20 }, (_, i) => i + 1);
    const gallery = seedGallery(ids);
    installRomBroadcast();

    emitUpdated(ids);
    await settle();

    expect(getRom).not.toHaveBeenCalled();
    expect(gallery.byPosition.size).toBe(0);
    expect(getRoms).toHaveBeenCalled();
  });

  it("invalidates on a delete, since positions shift", async () => {
    const gallery = seedGallery([10, 11]);
    installRomBroadcast();

    emitDeleted([10]);
    await settle();

    expect(getRom).not.toHaveBeenCalled();
    expect(gallery.byPosition.size).toBe(0);
    expect(getRoms).toHaveBeenCalled();
  });

  it("prefers the delete when a ROM is both updated and deleted in one window", async () => {
    const gallery = seedGallery([10]);
    installRomBroadcast();

    emitUpdated([10]);
    emitDeleted([10]);
    await settle();

    expect(getRom).not.toHaveBeenCalled();
    expect(gallery.byPosition.size).toBe(0);
  });
});
