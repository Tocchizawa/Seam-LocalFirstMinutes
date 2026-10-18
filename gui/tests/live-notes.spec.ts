import { test, expect, type Page } from "@playwright/test";

const harness = "/tests/live-notes-harness.html";
const editor = (page: Page) => page.locator('[data-block-id="block-a"] [contenteditable="true"]');
const fixture = (page: Page, name: string) => page.getByRole("button", { name, exact: true });
async function open(page: Page) {
  await page.goto(harness);
  await expect(page.getByRole("textbox", { name: "論点の見出し", exact: true }).first()).toHaveValue("認証方式");
}

test("new topics, topic return, and outline retain previous discussion", async ({ page }) => {
  await open(page);
  await fixture(page, "新話題").click();
  await expect(page.getByRole("textbox", { name: "論点の見出し", exact: true })).toHaveCount(2);
  await fixture(page, "話題復帰 / 別ブロック更新").click();
  await expect(page.locator('[data-topic-id="topic-a"] [data-block-id]')).toHaveCount(2);
  await expect(editor(page)).toHaveText("既存の認証基盤を利用する案");
  await expect(page.getByRole("navigation").getByRole("link")).toHaveCount(2);
});

test("AI update to another block preserves focused input, selection, and scroll", async ({ page }) => {
  await open(page);
  await editor(page).fill("人間の入力を保持する");
  await editor(page).press("ArrowLeft");
  const selectionBefore = await page.evaluate(() => ({ offset: window.getSelection()?.anchorOffset, scroll: document.querySelector(".live-note-document")?.scrollTop }));
  await fixture(page, "話題復帰 / 別ブロック更新").dispatchEvent("click");
  await expect(page.locator('[data-topic-id="topic-a"] [data-block-id]')).toHaveCount(2);
  await expect(editor(page)).toBeFocused();
  await expect(editor(page)).toHaveText("人間の入力を保持する");
  const selectionAfter = await page.evaluate(() => ({ offset: window.getSelection()?.anchorOffset, scroll: document.querySelector(".live-note-document")?.scrollTop }));
  expect(selectionAfter).toEqual(selectionBefore);
});

test("same-block stale update becomes conflict without losing manual draft", async ({ page }) => {
  await open(page);
  await editor(page).fill("11月中旬へ変更（PM確認済み）");
  await fixture(page, "同一ブロック競合").dispatchEvent("click");
  await expect(page.getByText("競合：入力を保持しています。最新の内容を確認して再保存してください。", { exact: true })).toBeVisible();
  await expect(editor(page)).toHaveText("11月中旬へ変更（PM確認済み）");
  expect(await page.evaluate(() => localStorage.getItem("seam:live-note-draft:v1:test-session:block:block-a"))).toContain("11月中旬");
  await page.getByRole("button", { name: "入力を最新の版へ再保存" }).click();
  await expect(page.getByText("手動編集 · AI上書き保護")).toBeVisible();
  await expect(editor(page)).toHaveText("11月中旬へ変更（PM確認済み）");
});

test("manual topic/block editing, reorder, deletion and history undo", async ({ page }) => {
  await open(page);
  await page.getByRole("textbox", { name: "新しい論点の見出し" }).fill("手動の議題");
  await page.getByRole("button", { name: "見出しを追加" }).click();
  await expect(page.getByRole("textbox", { name: "論点の見出し", exact: true })).toHaveCount(2);
  const manualTopic = page.locator('.live-note-topic').filter({ has: page.locator('input[value="手動の議題"]') });
  await manualTopic.getByRole("textbox", { name: "追加するメモ" }).fill("**手動のメモ**を残す");
  await manualTopic.getByRole("button", { name: "メモを追加", exact: true }).click();
  await expect(manualTopic.locator('[contenteditable="true"]')).toHaveText("手動のメモを残す");
  await manualTopic.getByRole("button", { name: "論点を上へ移動" }).click();
  await expect(page.getByRole("textbox", { name: "論点の見出し", exact: true }).first()).toHaveValue("手動の議題");
  page.once("dialog", (dialog) => dialog.accept());
  await manualTopic.getByRole("button", { name: "メモを削除" }).click();
  await expect(manualTopic.locator('[data-block-id]')).toHaveCount(0);
  await page.getByRole("tab", { name: "更新履歴" }).click();
  await page.getByRole("button", { name: "元に戻す", exact: true }).first().click();
  await page.getByRole("tab", { name: "ノート", exact: true }).click();
  await expect(manualTopic.locator('[contenteditable="true"]')).toHaveText("手動のメモを残す");
});

test("network loss and reload retain unsaved manual drafts; reconnect retries", async ({ page }) => {
  await open(page);
  await fixture(page, "接続断").click();
  await editor(page).fill("ネットワーク断中の手動入力");
  await expect(page.getByText(/未保存：Synthetic network disconnected/)).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.reload();
  await expect(editor(page)).toHaveText("ネットワーク断中の手動入力");
  await page.getByRole("button", { name: "保存を再試行", exact: true }).click();
  await expect(page.getByText("手動編集 · AI上書き保護")).toBeVisible();
});

test("WS reconnect snapshots avoid duplicate topics and blocks", async ({ page }) => {
  await open(page);
  await fixture(page, "接続断").click();
  await fixture(page, "新話題").click();
  await fixture(page, "話題復帰 / 別ブロック更新").click();
  await fixture(page, "再接続").click();
  await fixture(page, "再接続").click();
  await expect(page.getByRole("textbox", { name: "論点の見出し", exact: true })).toHaveCount(2);
  await expect(page.locator('[data-block-id]')).toHaveCount(2);
});

test("60-minute synthetic replay retains 22 topics and revisits original; bounded calls", async ({ page }) => {
  await open(page);
  await fixture(page, "60分相当リプレイ").click();
  await expect(page.getByRole("textbox", { name: "論点の見出し", exact: true })).toHaveCount(22);
  await expect(page.locator('[data-topic-id="topic-a"] [data-block-id]')).toHaveCount(40);
  await expect(page.getByText("未整理 0件 · 呼び出し 40回", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "更新履歴" }).click();
  await expect(page.getByRole("button", { name: /以前の履歴を読み込む/ })).toBeVisible();
});

test("slow CLI status does not block manual editing; stop preserves content and export", async ({ page }) => {
  await open(page);
  await fixture(page, "応答遅延切替").click();
  await page.getByRole("button", { name: "残りを整理 / 再試行" }).click();
  await editor(page).fill("処理待ちでも入力できる");
  await expect(editor(page)).toHaveText("処理待ちでも入力できる");
  await expect(page.getByText("手動編集 · AI上書き保護")).toBeVisible({ timeout: 10000 });
  await fixture(page, "停止").click();
  await expect(page.getByText("会議終了", { exact: true })).toBeVisible();
  await expect(editor(page)).toHaveText("処理待ちでも入力できる");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Markdown出力" }).click();
  expect((await download).suggestedFilename()).toBe("test-session-notes.md");
});

test("settings explain cloud transmission and require explicit per-provider consent", async ({ page }) => {
  await open(page);
  await fixture(page, "設定表示切替").click();
  await expect(page.getByRole("checkbox").first()).not.toBeChecked();
  await expect(page.getByText(/同意前はAIへ送信しません/)).toBeVisible();
  await page.getByRole("checkbox").first().check();
  await page.getByRole("checkbox").nth(1).check();
  await page.getByRole("combobox", { name: "ライブノートのCLI", exact: true }).selectOption("claude_code");
  await expect(page.getByRole("checkbox").nth(1)).not.toBeChecked();
  await page.getByRole("button", { name: "CLIの利用可否を確認（推論なし）" }).click();
  await expect(page.getByRole("status")).toContainText("制限されたCLI実行機能を確認しました");
});

test("live model selection preserves separate provider choices and inherited summary model", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await open(page);
  await fixture(page, "設定表示切替").click();
  const cli = page.getByRole("combobox", { name: "ライブノートのCLI", exact: true });
  const model = page.getByRole("combobox", { name: "ライブノートのモデル", exact: true });
  await expect(model).toHaveValue("");
  await expect(model.locator("option:checked")).toContainText("gpt-6-sol");
  await expect(model.locator('option[value="gpt-6-luna"]')).toContainText("軽量・ライブ推奨");
  await model.selectOption("gpt-6-luna");
  await expect(page.getByText("次の会議で使用：gpt-6-luna。録音後要約とは別のモデルを選べます。", { exact: true })).toBeVisible();
  await cli.selectOption("claude_code");
  await expect(model).toHaveValue("");
  await model.selectOption("haiku");
  await cli.selectOption("codex");
  await expect(model).toHaveValue("gpt-6-luna");
  await expect(page.getByText("録音中の方式・provider・modelは会議開始時の設定を維持します。", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/live-models-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page, "テーマ切替").click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(model).toBeVisible();
  await page.screenshot({ path: "test-results/live-models-mobile-dark.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("empty CLI cache allows manual live model IDs and returning to inheritance", async ({ page }) => {
  await page.goto(`${harness}?empty-models`);
  await fixture(page, "設定表示切替").click();
  const model = page.getByRole("combobox", { name: "ライブノートのモデル", exact: true });
  await model.selectOption("__custom__");
  const id = page.getByLabel("ライブノートのモデルID", { exact: true });
  await id.fill("gpt-6-luna");
  await fixture(page, "設定表示切替").click();
  await fixture(page, "設定表示切替").click();
  await expect(id).toHaveValue("gpt-6-luna");
  await model.selectOption("");
  await expect(id).toHaveCount(0);
  await expect(model).toHaveValue("");
});

test("mobile and dark theme have no horizontal overflow and retain usable editing", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page);
  await fixture(page, "テーマ切替").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await editor(page).fill("スマートフォン幅でも編集できる");
  await expect(editor(page)).toHaveText("スマートフォン幅でも編集できる");
  await page.screenshot({ path: "test-results/live-notes-mobile-dark.png", fullPage: true });
});


test("idle AI block update stays AI-owned and does not create a manual draft", async ({ page }) => {
  await open(page);
  await fixture(page, "同一ブロック競合").click();
  await expect(editor(page)).toHaveText("AIから遅れて届いた更新");
  await page.waitForTimeout(700);
  await expect(page.getByText("手動編集 · AI上書き保護")).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("seam:live-note-draft:v1:test-session:block:block-a"))).toBe(null);
});


test("IME composition keeps a durable draft without saving unfinished Japanese text", async ({ page }) => {
  await open(page);
  await editor(page).dispatchEvent("compositionstart", {data:"へんかん"});
  await editor(page).fill("変換途中");
  await page.waitForTimeout(650);
  await expect(page.getByText("手動編集 · AI上書き保護")).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("seam:live-note-draft:v1:test-session:block:block-a"))).toContain("変換途中");
  await fixture(page, "話題復帰 / 別ブロック更新").dispatchEvent("click");
  await expect(editor(page)).toHaveText("変換途中");
  await editor(page).dispatchEvent("compositionend", {data:"変換完了"});
  await expect(page.getByText("手動編集 · AI上書き保護")).toBeVisible();
});

test("events during slow snapshot resync are retained and older snapshots cannot rewind mutations", async ({ page }) => {
  await open(page);
  await fixture(page, "遅いスナップショット").click();
  await fixture(page, "新話題").click();
  await page.getByRole("textbox", {name:"新しい論点の見出し"}).fill("再同期中の手動追加");
  await page.getByRole("button", {name:"見出しを追加"}).click();
  await expect(page.getByRole("textbox", {name:"論点の見出し",exact:true})).toHaveCount(3);
  await page.waitForTimeout(600);
  await expect(page.getByRole("textbox", {name:"論点の見出し",exact:true})).toHaveCount(3);
});

test("legacy minutes still display and edit summary and transcript with live mode absent", async ({ page }) => {
  await open(page);
  await fixture(page, "従来議事録切替").click();
  await expect(page.getByText("既存の議事録を表示します。", {exact:true})).toBeVisible();
  await page.getByRole("button", {name:"編集",exact:true}).click();
  await page.locator('[contenteditable="true"]').fill("従来の要約を手動編集");
  await page.getByRole("button", {name:"保存",exact:true}).click();
  await expect(page.getByText("従来の要約を手動編集", {exact:true})).toBeVisible();
  await expect(page.getByRole("button", {name:"再要約",exact:true})).toBeVisible();
  await page.getByRole("button", {name:/Transcribe/}).click();
  await expect(page.getByText("従来の文字起こし本文",{exact:true})).toBeVisible();
});

test("desktop layout screenshot", async ({page}) => {
  await open(page);
  await fixture(page,"新話題").click();
  await fixture(page,"話題復帰 / 別ブロック更新").click();
  await page.screenshot({path:"test-results/live-notes-desktop-light.png",fullPage:true});
});


test("new topic and new block drafts survive navigation and reload", async ({page}) => {
  await open(page);
  await page.getByRole("textbox", {name:"新しい論点の見出し"}).fill("未追加の見出し");
  await page.getByRole("textbox", {name:"追加するメモ"}).fill("まだ追加していないメモ");
  await fixture(page,"設定表示切替").click();
  await fixture(page,"設定表示切替").click();
  await expect(page.getByRole("textbox", {name:"新しい論点の見出し"})).toHaveValue("未追加の見出し");
  await expect(page.getByRole("textbox", {name:"追加するメモ"})).toHaveValue("まだ追加していないメモ");
  await page.reload();
  await expect(page.getByRole("textbox", {name:"新しい論点の見出し"})).toHaveValue("未追加の見出し");
  await expect(page.getByRole("textbox", {name:"追加するメモ"})).toHaveValue("まだ追加していないメモ");
});

test("new keystrokes during an in-flight save are saved at the acknowledged revision", async ({page}) => {
  await open(page);
  await fixture(page,"応答遅延切替").click();
  await editor(page).fill("先の編集");
  await expect(page.locator('[data-block-id="block-a"] .live-note-save-state')).toContainText("保存中");
  await editor(page).fill("保存待ち中の次の編集");
  await expect(page.locator('[data-block-id="block-a"] .live-note-save-state')).toContainText("保存済み",{timeout:10000});
  await page.waitForTimeout(2500);
  await expect(editor(page)).toHaveText("保存待ち中の次の編集");
  expect(await page.evaluate(() => localStorage.getItem("seam:live-note-draft:v1:test-session:block:block-a"))).toBe(null);
});


test("leaving the note before debounce flushes manual edits without dropping the draft", async ({page}) => {
  await open(page);
  await editor(page).fill("画面を移動する直前の入力");
  await fixture(page,"設定表示切替").click();
  await fixture(page,"設定表示切替").click();
  await expect(editor(page)).toHaveText("画面を移動する直前の入力");
  await expect(page.getByText("手動編集 · AI上書き保護")).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("seam:live-note-draft:v1:test-session:block:block-a"))).toBe(null);
});


test("delayed save from an unmounted editor cannot clear a remounted editor's newer durable draft", async ({page}) => {
  await open(page);
  await fixture(page,"応答遅延切替").click();
  await editor(page).fill("移動前の編集");
  await fixture(page,"設定表示切替").click();
  await fixture(page,"設定表示切替").click();
  await page.waitForTimeout(900);
  await editor(page).fill("再表示後の新しい編集");
  await page.waitForTimeout(1300);
  expect(await page.evaluate(() => localStorage.getItem("seam:live-note-draft:v1:test-session:block:block-a"))).toContain("再表示後の新しい編集");
  await expect(editor(page)).toHaveText("再表示後の新しい編集");
});
