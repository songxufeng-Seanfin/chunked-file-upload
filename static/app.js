/* 大文件分片上传 · 浏览器端（Vue 3 全局构建，无打包、无 npm）
 *
 * 浏览器做的事：算整文件指纹 → 问服务器 → 切片并发上传 → 请求合并。
 * 服务器做的事：校验每一片、拦重复、按序拼接、整文件再校验一次。
 */
(function () {
  "use strict";
  const { createApp, ref, computed, onMounted } = Vue;

  const API = {
    state: "/api/state/",
    check: "/api/upload/check/",
    chunk: "/api/upload/chunk/",
    merge: "/api/upload/merge/",
    demo: "/api/demo/run/",
    reset: "/api/demo/reset/",
  };

  function csrf() {
    const hit = document.cookie.split("; ").find((c) => c.startsWith("csrftoken="));
    return hit ? decodeURIComponent(hit.split("=")[1]) : "";
  }

  function human(num) {
    num = Number(num || 0);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (num >= 1024 && i < units.length - 1) { num /= 1024; i++; }
    return i === 0 ? `${num} B` : `${num.toFixed(1)} ${units[i]}`;
  }

  async function sha256(buffer) {
    if (!window.crypto || !crypto.subtle) {
      throw new Error("当前页面不是安全上下文，请用 https 或 127.0.0.1 / localhost 打开");
    }
    const digest = await crypto.subtle.digest("SHA-256", buffer);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  async function postJSON(url, data) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(data || {}),
    });
    const json = await res.json().catch(() => ({ ok: false, error: "服务端返回的不是 JSON" }));
    if (!res.ok || json.ok === false) throw new Error(json.error || `HTTP ${res.status}`);
    return json;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  createApp({
    setup() {
      const state = ref({ stats: {}, files: [], tasks: [], events: [], limits: {} });
      const busy = ref(false);
      const notice = ref({ text: "", type: "" });
      const dragging = ref(false);

      const picked = ref(null);
      const fileHash = ref("");
      const uploading = ref(false);
      const paused = ref(false);
      const interrupted = ref(false);
      const progress = ref(0);
      const sent = ref(0);
      const phase = ref("等待选择文件");
      const chunkStates = ref([]);
      const currentTask = ref("");

      const stats = computed(() => state.value.stats || {});
      const files = computed(() => state.value.files || []);
      const tasks = computed(() => state.value.tasks || []);
      const events = computed(() => state.value.events || []);
      const limits = computed(() => {
        const raw = state.value.limits || {};
        const cs = raw.chunk_size || 2 * 1024 * 1024;
        return {
          chunk_size: cs,
          chunk_size_h: human(cs),
          demo_chunk_size: raw.demo_chunk_size || 256 * 1024,
          demo_chunk_size_h: human(raw.demo_chunk_size || 256 * 1024),
          concurrency: raw.concurrency || 3,
          max_size: raw.max_size || 0,
        };
      });
      const expectedTotal = computed(() =>
        picked.value ? Math.max(1, Math.ceil(picked.value.size / limits.value.chunk_size)) : 0
      );
      const doneCount = computed(() =>
        chunkStates.value.filter((c) => c.state === "done" || c.state === "dup").length
      );

      function toast(text, type) {
        notice.value = { text, type: type || "" };
        clearTimeout(toast._timer);
        toast._timer = setTimeout(() => { notice.value = { text: "", type: "" }; }, 7000);
      }

      async function loadState() {
        const res = await fetch(API.state);
        const data = await res.json();
        if (data.ok) state.value = data;
      }

      async function runDemo(scenario) {
        if (busy.value) return;
        busy.value = true;
        try {
          const data = await postJSON(API.demo, { scenario });
          toast(data.message, scenario === "instant" ? "info" : "");
          await loadState();
        } catch (err) {
          toast("演示失败：" + err.message, "error");
        } finally {
          busy.value = false;
        }
      }

      async function resetAll() {
        if (busy.value) return;
        if (!window.confirm("清空所有演示数据（文件档案、任务、分片、事件）？")) return;
        busy.value = true;
        try {
          const data = await postJSON(API.reset, {});
          picked.value = null; fileHash.value = ""; chunkStates.value = [];
          progress.value = 0; sent.value = 0; interrupted.value = false;
          phase.value = "等待选择文件";
          toast(data.message, "info");
          await loadState();
        } catch (err) {
          toast("清空失败：" + err.message, "error");
        } finally {
          busy.value = false;
        }
      }

      function chooseFile() {
        const input = document.getElementById("picker");
        if (input) input.click();
      }

      function prepare(file) {
        if (!file) return;
        picked.value = file;
        fileHash.value = "";
        chunkStates.value = [];
        progress.value = 0;
        sent.value = 0;
        interrupted.value = false;
        paused.value = false;
        phase.value = "已选文件，等待开始";
        toast(`已选择 ${file.name}（${human(file.size)}），预计切成 ${Math.ceil(file.size / limits.value.chunk_size)} 片`, "info");
      }

      function onPick(event) {
        prepare(event.target.files && event.target.files[0]);
        event.target.value = ""; // 允许重复选同一个文件
      }

      function onDrop(event) {
        dragging.value = false;
        prepare(event.dataTransfer.files && event.dataTransfer.files[0]);
      }

      async function sendOne(index) {
        const size = limits.value.chunk_size;
        const start = index * size;
        const blob = picked.value.slice(start, Math.min(picked.value.size, start + size));
        const chunkHash = await sha256(await blob.arrayBuffer());
        chunkStates.value[index].state = "doing";

        const form = new FormData();
        form.append("task_id", currentTask.value);
        form.append("index", String(index));
        form.append("chunk_hash", chunkHash);
        form.append("file", blob, `${picked.value.name}.part${index}`);

        const res = await fetch(API.chunk, {
          method: "POST", headers: { "X-CSRFToken": csrf() }, body: form,
        });
        const data = await res.json().catch(() => ({ ok: false, error: "返回的不是 JSON" }));
        if (!res.ok || data.ok === false) throw new Error(data.error || `第 ${index} 片失败`);
        if (data.result === "APPLIED") {
          chunkStates.value[index].state = "done";
          sent.value += blob.size;
        } else if (data.result === "DUPLICATE") {
          chunkStates.value[index].state = "dup";
        } else {
          throw new Error(data.message || `第 ${index} 片被拒绝`);
        }
        progress.value = Math.round(doneCount.value * 100 / Math.max(1, chunkStates.value.length));
      }

      async function startUpload() {
        if (!picked.value || uploading.value) return;
        uploading.value = true;
        paused.value = false;
        interrupted.value = false;
        try {
          phase.value = "计算整文件指纹";
          const whole = await sha256(await picked.value.arrayBuffer());
          fileHash.value = whole;

          phase.value = "上传前检查（秒传 / 断点）";
          const checked = await postJSON(API.check, {
            file_hash: whole,
            filename: picked.value.name,
            size: picked.value.size,
            chunk_size: limits.value.chunk_size,
          });
          if (checked.instant) {
            progress.value = 100;
            phase.value = "秒传完成 · 0 字节传输";
            chunkStates.value = Array.from({ length: Math.min(expectedTotal.value, 60) },
              (_, i) => ({ i, state: "done" }));
            toast(checked.message);
            return;
          }

          currentTask.value = checked.task_id;
          const total = checked.total_chunks;
          const have = new Set(checked.uploaded || []);
          chunkStates.value = Array.from({ length: Math.min(total, 60) },
            (_, i) => ({ i, state: have.has(i) ? "done" : "todo" }));
          progress.value = Math.round(have.size * 100 / total);
          phase.value = checked.resumed ? "断点续传 · 只补缺片" : "分片并发上传中";
          if (checked.resumed) toast(checked.message, "info");

          const queue = [];
          for (let i = 0; i < total; i++) if (!have.has(i)) queue.push(i);

          const worker = async () => {
            while (queue.length) {
              if (interrupted.value) return;
              while (paused.value && !interrupted.value) await sleep(120);
              if (interrupted.value) return;
              const index = queue.shift();
              await sendOne(index);
            }
          };
          await Promise.all(
            Array.from({ length: Math.min(limits.value.concurrency, queue.length || 1) }, worker)
          );

          if (interrupted.value) {
            phase.value = "已中断 · 可点「续传」继续";
            toast("已模拟中断，已落盘的分片会保留；点「续传」只补缺的片", "info");
            return;
          }

          phase.value = "合并分片并校验整文件指纹";
          const merged = await postJSON(API.merge, { task_id: checked.task_id });
          if (merged.result === "INCOMPLETE") {
            throw new Error(`${merged.message}：${(merged.missing || []).join(", ")}`);
          }
          progress.value = 100;
          phase.value = "完成 · 整文件指纹校验通过";
          toast(merged.message || "上传完成");
        } catch (err) {
          phase.value = "出错";
          toast("上传失败：" + err.message, "error");
        } finally {
          uploading.value = false;
          await loadState();
        }
      }

      function interrupt() {
        interrupted.value = true;
        paused.value = false;
        toast("正在停止取新分片……已在途的分片仍会落盘，这正是断点续传要处理的真实情况", "info");
      }

      function statusClass(task) {
        if (task.status === "failed") return "bad";
        if (task.status === "uploading") return "warn";
        return "ok";
      }

      function resultClass(result) {
        if (["APPLIED", "MERGED", "VERIFIED", "INSTANT", "RESUMED"].includes(result)) return "ok";
        if (["DUPLICATE", "CONFLICT"].includes(result)) return "dup";
        if (["CORRUPT", "REJECTED"].includes(result)) return "bad";
        return "muted";
      }

      onMounted(loadState);
      return {
        state, busy, notice, dragging, picked, fileHash, uploading, paused, interrupted,
        progress, sent, phase, chunkStates, stats, files, tasks, events, limits,
        expectedTotal, doneCount, toast, loadState, runDemo, resetAll, chooseFile,
        onPick, onDrop, startUpload, interrupt, statusClass, resultClass, human,
      };
    },
  }).mount("#app");
})();
