// Adapted from Farm Manager bffVault.ts; same encrypted session / fixed authority contract.
/** Durable, encrypted whole-record storage for the server-owned Farm session. */
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
export function createBffVault(directory, encodedKey) {
    const key = Buffer.from(encodedKey, "base64");
    if (key.length !== 32 || key.toString("base64") !== encodedKey)
        throw new Error("TRACKER_BFF_VAULT_KEY must encode exactly 32 random bytes");
    if (!path.isAbsolute(directory))
        throw new Error("BFF vault path must be absolute");
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const directoryStat = fs.lstatSync(directory);
    if (!directoryStat.isDirectory() || directoryStat.isSymbolicLink() || directoryStat.mode & 0o077)
        throw new Error("BFF vault directory must be private (0700)");
    const idPattern = /^[A-Za-z0-9_-]{43}$/;
    function filename(id) {
        if (!idPattern.test(id))
            throw new Error("Invalid session handle");
        return path.join(directory, `${crypto.createHash("sha256").update(id).digest("hex")}.json`);
    }
    function load(id) {
        return loadFile(filename(id));
    }
    function loadFile(file) {
        let fd;
        try {
            fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
        }
        catch (error) {
            if (error.code === "ENOENT")
                return null;
            throw error;
        }
        try {
            const stat = fs.fstatSync(fd);
            if (!stat.isFile() || stat.size > 128 * 1024 || stat.mode & 0o077)
                throw new Error("Unsafe BFF session record");
            const envelope = JSON.parse(fs.readFileSync(fd, "utf8"));
            if (envelope.v !== 1)
                throw new Error("Unknown BFF vault format");
            const decipher = crypto.createDecipheriv("aes-256-gcm", key, Buffer.from(envelope.iv, "base64"));
            decipher.setAAD(Buffer.from(path.basename(file)));
            decipher.setAuthTag(Buffer.from(envelope.tag, "base64"));
            const record = JSON.parse(Buffer.concat([
                decipher.update(Buffer.from(envelope.body, "base64")),
                decipher.final()
            ]).toString("utf8"));
            if (!Number.isSafeInteger(record.createdAt) ||
                record.createdAt > Date.now() ||
                !Object.hasOwn(record, "tokens"))
                throw new Error("Invalid protected session record");
            return record;
        }
        finally {
            fs.closeSync(fd);
        }
    }
    function save(id, record) {
        const file = filename(id), temporary = `${file}.${crypto.randomUUID()}.tmp`;
        const iv = crypto.randomBytes(12), cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
        cipher.setAAD(Buffer.from(path.basename(file)));
        const body = Buffer.concat([cipher.update(JSON.stringify(record)), cipher.final()]);
        const encoded = JSON.stringify({
            v: 1,
            iv: iv.toString("base64"),
            tag: cipher.getAuthTag().toString("base64"),
            body: body.toString("base64")
        });
        if (Buffer.byteLength(encoded) > 128 * 1024)
            throw new Error("BFF session exceeds storage bound");
        let fd;
        try {
            fd = fs.openSync(temporary, "wx", 0o600);
            fs.writeFileSync(fd, encoded);
            fs.fsyncSync(fd);
            fs.closeSync(fd);
            fd = undefined;
            fs.renameSync(temporary, file);
            const dir = fs.openSync(directory, "r");
            try {
                fs.fsyncSync(dir);
            }
            finally {
                fs.closeSync(dir);
            }
        }
        finally {
            if (fd !== undefined)
                fs.closeSync(fd);
            try {
                fs.unlinkSync(temporary);
            }
            catch (error) {
                if (error.code !== "ENOENT")
                    throw error;
            }
        }
    }
    return {
        load,
        save,
        sweep({ maxAge, excludeIds = [], now = Date.now() }) {
            const excluded = new Set(excludeIds.map(filename));
            let removed = 0;
            for (const name of fs.readdirSync(directory)) {
                if (!/^[a-f0-9]{64}\.json$/.test(name)) continue;
                const file = path.join(directory, name);
                if (excluded.has(file)) continue;
                try {
                    const record = loadFile(file);

                    if (record?.tokens === null && record.createdAt + maxAge <= now) {
                        fs.unlinkSync(file); removed++;
                    }
                } catch {                                                             }
            }
            if (removed) {
                const fd = fs.openSync(directory, 'r');
                try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
            }
            return removed;
        },
        create() {
            const id = crypto.randomBytes(32).toString("base64url");
            save(id, { createdAt: Date.now(), tokens: null });
            return id;
        },
        remove(id) {
            const file = filename(id);
            fs.rmSync(file, { force: true });
            const fd = fs.openSync(directory, "r");
            try {
                fs.fsyncSync(fd);
            }
            finally {
                fs.closeSync(fd);
            }
        }
    };
}
