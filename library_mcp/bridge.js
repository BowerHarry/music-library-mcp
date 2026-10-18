// Bridge between the Python server and the Music app (JavaScript for Automation).
//   osascript -l JavaScript bridge.js <request.json>
// request: {"op": "...", ...args}. Prints {"ok": true, "result": ...} or {"ok": false, "error": "..."}.
// Every op talks to Music through Apple Events: nothing is clicked and no window is touched.
ObjC.import("Foundation");

const M = Application("Music");
const FM = $.NSFileManager.defaultManager;

// Track properties we read and (where marked) write. JS name -> Music property.
const FIELDS = {
  name: "name", artist: "artist", album: "album", albumArtist: "albumArtist", genre: "genre",
  year: "year", trackNumber: "trackNumber", trackCount: "trackCount", discNumber: "discNumber",
  discCount: "discCount", compilation: "compilation", composer: "composer", comment: "comment",
  grouping: "grouping", sortName: "sortName", sortArtist: "sortArtist", sortAlbum: "sortAlbum",
  sortAlbumArtist: "sortAlbumArtist", rating: "rating",
};
const READ_ONLY = {
  id: "persistentID", kind: "kind", cloudStatus: "cloudStatus", bitRate: "bitRate",
  sampleRate: "sampleRate", size: "size", duration: "duration", playedCount: "playedCount",
  dateAdded: "dateAdded",
};

function readFile(p) {
  return $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, null).js;
}

function lib() { return M.libraryPlaylists[0]; }

function trackById(id) {
  const found = lib().tracks.whose({ persistentID: id })();
  if (!found.length) throw new Error("track not found: " + id);
  return found[0];
}

function playlistById(pid) {
  const found = M.userPlaylists.whose({ persistentID: pid })();
  if (!found.length) throw new Error("playlist not found: " + pid);
  return found[0];
}

function iso(d) { return d ? d.toISOString() : null; }

function getFav(t) {
  try { return t.favorited(); } catch (e) { try { return t.loved(); } catch (e2) { return null; } }
}

function setFav(t, v) {
  try { t.favorited = !!v; } catch (e) { t.loved = !!v; }
}

function location(t) {
  try { const l = t.location(); return l ? l.toString() : null; } catch (e) { return null; }
}

function exists(p) { return !!p && FM.fileExistsAtPath(p); }

function details(t) {
  const out = {};
  for (const [k, p] of Object.entries(FIELDS)) { try { out[k] = t[p](); } catch (e) { out[k] = null; } }
  for (const [k, p] of Object.entries(READ_ONLY)) { try { out[k] = t[p](); } catch (e) { out[k] = null; } }
  out.dateAdded = iso(out.dateAdded);
  out.favorited = getFav(t);
  out.location = location(t);
  out.fileMissing = out.location ? !exists(out.location) : null;
  try { out.artworkCount = t.artworks.length; } catch (e) { out.artworkCount = null; }
  try { out.playedDate = iso(t.playedDate()); } catch (e) { out.playedDate = null; }
  out.playlists = userPlaylistsOf(t).map(p => ({ id: p.persistentID(), name: p.name() }));
  return out;
}

function userPlaylistsOf(t) {
  try {
    return t.playlists().filter(p => {
      try { return p.class() === "userPlaylist" && !p.smart() && p.specialKind() === "none"; }
      catch (e) { return false; }
    });
  } catch (e) { return []; }
}

// ------------------------------------------------------------------ ops
const OPS = {
  ping() {
    return { musicRunning: M.running(), version: M.version(), tracks: lib().tracks.length };
  },

  // Every library track, column by column (one Apple Event per column, so it's fast).
  dump() {
    const t = lib().tracks;
    const cols = {}, missing = [];
    const all = Object.assign({}, READ_ONLY, FIELDS);
    for (const [k, p] of Object.entries(all)) {
      try { cols[k] = t[p](); } catch (e) { cols[k] = null; missing.push(k); }
    }
    if (cols.dateAdded) cols.dateAdded = cols.dateAdded.map(iso);
    // Locations come from the library's file tracks only: entries without a local file (cloud-only
    // purchases, or ghosts left behind when a file was replaced) would make a bulk read fail.
    try {
      const ft = lib().fileTracks;
      const fids = ft.persistentID(), flocs = ft.location();
      const byId = {};
      for (let i = 0; i < fids.length; i++) byId[fids[i]] = flocs[i] ? flocs[i].toString() : null;
      cols.hasFile = cols.id.map(id => Object.prototype.hasOwnProperty.call(byId, id));
      cols.location = cols.id.map(id => byId[id] || null);
    } catch (e) { cols.location = null; cols.hasFile = null; missing.push("location: " + e); }
    try { cols.favorited = t.favorited(); } catch (e) {
      try { cols.favorited = t.loved(); } catch (e2) { cols.favorited = null; missing.push("favorited"); }
    }
    return { count: cols.id.length, cols: cols, missing: missing };
  },

  get(req) { return req.ids.map(id => { try { return details(trackById(id)); } catch (e) { return { id: id, error: String(e) }; } }); },

  // edits: [{id, changes: {field: value}}]. Returns the values before and after each change.
  set(req) {
    return req.edits.map(ed => {
      const r = { id: ed.id, before: {}, after: {} };
      try {
        const t = trackById(ed.id);
        for (const [k, v] of Object.entries(ed.changes)) {
          if (k === "favorited") {
            r.before[k] = getFav(t); setFav(t, v); r.after[k] = getFav(t); continue;
          }
          const p = FIELDS[k];
          if (!p) throw new Error("not editable: " + k);
          r.before[k] = t[p]();
          t[p] = v;
          r.after[k] = t[p]();
        }
      } catch (e) { r.error = String(e); }
      return r;
    });
  },

  playlists() {
    const P = M.userPlaylists;
    const ids = P.persistentID(), names = P.name(), smart = P.smart(), kinds = P.specialKind();
    let classes = []; try { classes = P.class(); } catch (e) {}
    const out = [];
    for (let i = 0; i < ids.length; i++) {
      if (kinds[i] !== "none") continue;
      const folder = classes[i] === "folderPlaylist";
      let count = null;
      if (!folder) { try { count = P[i].tracks.length; } catch (e) {} }
      out.push({ id: ids[i], name: names[i], smart: smart[i], folder: folder, count: count });
    }
    return out;
  },

  playlistTracks(req) {
    const p = playlistById(req.id);
    return { id: req.id, name: p.name(), smart: p.smart(), trackIds: p.tracks.persistentID() };
  },

  createPlaylist(req) {
    let p;
    try { p = M.UserPlaylist({ name: req.name }).make(); }
    catch (e) { p = M.make({ new: "userPlaylist", withProperties: { name: req.name } }); }
    return { id: p.persistentID(), name: p.name() };
  },

  renamePlaylist(req) {
    const p = playlistById(req.id);
    const before = p.name();
    p.name = req.name;
    return { id: req.id, before: before, after: p.name() };
  },

  // Only used to undo createPlaylist, and only while the playlist is still empty.
  deleteEmptyPlaylist(req) {
    const p = playlistById(req.id);
    if (p.tracks.length) throw new Error("playlist is not empty");
    M.delete(p);
    return { id: req.id, deleted: true };
  },

  addToPlaylist(req) {
    const p = playlistById(req.id);
    if (p.smart()) throw new Error("smart playlists can't be edited");
    const have = new Set(p.tracks.persistentID());
    const added = [], skipped = [], errors = [];
    for (const id of req.trackIds) {
      if (have.has(id)) { skipped.push(id); continue; }
      try { M.duplicate(trackById(id), { to: p }); added.push(id); have.add(id); }
      catch (e) { errors.push({ id: id, error: String(e) }); }
    }
    return { added: added, alreadyThere: skipped, errors: errors };
  },

  // Removes the tracks from this playlist only; they stay in the library.
  removeFromPlaylist(req) {
    const p = playlistById(req.id);
    if (p.smart()) throw new Error("smart playlists can't be edited");
    const removed = [], notThere = [];
    for (const id of req.trackIds) {
      const hits = p.tracks.whose({ persistentID: id })();
      if (!hits.length) { notThere.push(id); continue; }
      for (const h of hits) M.delete(h);
      removed.push(id);
    }
    return { removed: removed, notThere: notThere };
  },

  // Add files to the library (Music copies/organises them per its own settings).
  add(req) {
    return req.paths.map(p => {
      const r = { path: p };
      try {
        if (!exists(p)) throw new Error("file not found");
        const added = M.add(Path(p));
        const nt = Array.isArray(added) ? added[0] : added;
        r.newId = nt.persistentID(); r.name = nt.name(); r.location = location(nt);
      } catch (e) { r.error = String(e); }
      return r;
    });
  },

  // Undo for add: remove library entries but leave the files exactly where they are.
  removeEntries(req) {
    return req.ids.map(id => {
      try { M.delete(trackById(id)); return { id: id, removed: true }; }
      catch (e) { return { id: id, error: String(e) }; }
    });
  },

  // Remove from the library, then move the file to the Bin. With iCloud Music Library on, Music's
  // scripting can only remove the local copy of a song that's in iCloud: the entry stays as a
  // cloud-only song. So check afterwards, and if the entry survived, add the file straight back
  // (Music merges it into that entry) and report it instead of binning the file.
  delete(req) {
    return req.ids.map(id => {
      const r = { id: id };
      try {
        const t = trackById(id);
        r.name = t.name(); r.artist = t.artist(); r.album = t.album(); r.cloudStatus = t.cloudStatus();
        r.location = location(t);
        M.delete(t);
        delay(0.5);
        const still = lib().tracks.whose({ persistentID: id })().length > 0;
        r.removedFromLibrary = !still;
        if (still) {
          r.fileBinned = false;
          if (r.location && exists(r.location)) {
            const added = M.add(Path(r.location));
            const nt = Array.isArray(added) ? added[0] : added;
            r.restoredId = nt.persistentID();
          }
          r.note = "Music kept this song in iCloud Music Library (scripting can't remove it from iCloud); " +
                   "the file was put back. Delete it in the Music app instead.";
          return r;
        }
        if (r.location && exists(r.location)) {
          r.fileBinned = !!FM.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(r.location), null, null);
        } else {
          r.fileBinned = false;
          r.note = "no local file";
        }
      } catch (e) { r.error = String(e); }
      return r;
    });
  },

  // Undo for delete: move each file back from the Bin and add it to the library again.
  restore(req) {
    return req.items.map(it => {
      const r = { originalPath: it.originalPath };
      try {
        if (!exists(it.originalPath)) {
          if (!it.binPath || !exists(it.binPath)) throw new Error("file is no longer in the Bin");
          const dir = $(it.originalPath).stringByDeletingLastPathComponent;
          FM.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(dir, true, $(), null);
          if (!FM.moveItemAtPathToPathError(it.binPath, it.originalPath, null)) throw new Error("move back failed");
        }
        const added = M.add(Path(it.originalPath));
        const nt = Array.isArray(added) ? added[0] : added;
        r.newId = nt.persistentID();
      } catch (e) { r.error = String(e); }
      return r;
    });
  },

  // Re-add tracks so iCloud matches/uploads them afresh, keeping the file, rating, plays,
  // favourite and playlists. The file is backed up first and restored if Music moves it.
  rematch(req) {
    const app = Application.currentApplication();
    app.includeStandardAdditions = true;
    const q = s => "'" + String(s).replace(/'/g, "'\\''") + "'";
    const sh = cmd => app.doShellScript(cmd);
    return req.ids.map(id => {
      const r = { id: id, steps: [] };
      try {
        const t = trackById(id);
        const loc = location(t);
        if (!loc || !exists(loc)) throw new Error("no local file to re-add");
        r.name = t.name(); r.cloudBefore = t.cloudStatus();
        const meta = { rating: t.rating(), playedCount: t.playedCount() };
        const fav = getFav(t);
        const pls = userPlaylistsOf(t).map(p => p.persistentID());
        const i = loc.indexOf("/Media.localized/");
        const backupDir = req.backupDir || ((i > 0 ? loc.substring(0, i) : loc.substring(0, loc.lastIndexOf("/"))) + "/.library-mcp-rematch-backup");
        const backup = backupDir + "/" + id + "." + loc.split(".").pop();
        sh("mkdir -p " + q(backupDir) + " && cp -p " + q(loc) + " " + q(backup) + " && cmp -s " + q(loc) + " " + q(backup));
        r.steps.push("backed up");
        M.delete(t);
        r.steps.push("removed library entry");
        delay(1);
        if (!exists(loc)) {
          sh("mkdir -p " + q(loc.substring(0, loc.lastIndexOf("/"))) + " && cp -p " + q(backup) + " " + q(loc));
          r.steps.push("restored file from backup");
        }
        const added = M.add(Path(loc));
        const nt = Array.isArray(added) ? added[0] : added;
        r.newId = nt.persistentID();
        try { nt.rating = meta.rating; nt.playedCount = meta.playedCount; } catch (e) { r.warning = String(e); }
        if (fav) setFav(nt, true);
        for (const pid of pls) { try { M.duplicate(nt, { to: playlistById(pid) }); } catch (e) {} }
        r.playlistsRestored = pls.length;
        sh("rm -f " + q(backup));
        r.ok = true;
      } catch (e) { r.error = String(e); }
      return r;
    });
  },
};

function run(argv) {
  let req;
  try {
    req = JSON.parse(readFile(argv[0]));
    const fn = OPS[req.op];
    if (!fn) throw new Error("unknown op: " + req.op);
    return JSON.stringify({ ok: true, result: fn(req) });
  } catch (e) {
    return JSON.stringify({ ok: false, error: String(e) });
  }
}
