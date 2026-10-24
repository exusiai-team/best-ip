import { readdir } from "node:fs/promises";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

async function checkDirectory(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      await checkDirectory(path);
    } else if (entry.isFile() && /\.(?:c?js|mjs)$/u.test(entry.name)) {
      const result = spawnSync(process.execPath, ["--check", path], { stdio: "inherit" });
      if (result.error) throw result.error;
      if (result.status !== 0) process.exit(result.status || 1);
      console.log("Checked " + path);
    }
  }
}

// frontend-next 是「不经过打包器直接上线」的源码目录，所以逐个文件过 node --check。
for (const directory of ["frontend-next", "worker", "scripts"]) await checkDirectory(directory);
