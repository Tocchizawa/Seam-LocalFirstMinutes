import { test, expect } from "@playwright/test";

test("one document editor keeps section identity while editing and autosaving", async ({ page }) => {
  await page.goto("/tests/live-notes-harness.html");
  const editor = page.locator(".seam-document-editor[contenteditable=true]");
  await expect(editor).toHaveCount(1);
  await expect(page.getByRole("navigation").getByRole("button", { name: "認証方式" })).toBeVisible();
  await editor.fill("人が追記した結論\n\n次の段落です。");
  await expect(editor).toContainText("次の段落です。");
  await expect(page.getByRole("status").filter({ hasText: "保存" }).last()).toContainText("保存済み", { timeout: 4000 });
  await expect(editor).toHaveCount(1);
});

test("the shared editor renders Markdown paragraphs, code, lists and tables", async ({ page }) => {
  await page.goto("/tests/live-notes-harness.html");
  await page.getByRole("button", { name: "Markdown構造fixture" }).click();
  const editor = page.locator(".seam-document-editor[contenteditable=true]");
  await expect(editor.locator("strong")).toContainText("要点");
  await expect(editor.locator("pre code")).toContainText("const ready = true");
  await expect(editor.getByRole("table")).toBeVisible();
  await expect(editor.getByRole("row")).toHaveCount(3);
  await expect(editor.locator("[data-semantic-unit-id]")).toHaveCount(2);
});

test("remote changes to another semantic unit leave the active selection and scroll in place", async ({ page }) => {
  await page.goto("/tests/live-notes-harness.html");
  await page.getByRole("button", { name: "Markdown構造fixture" }).click();
  const editor = page.locator(".seam-document-editor[contenteditable=true]");
  await editor.click();
  await page.keyboard.press("End");
  await page.getByRole("button", { name: "新話題" }).dispatchEvent("click");
  await expect(editor.locator("[data-semantic-unit-id]")).toHaveCount(3);
  await expect(editor.locator("table")).toBeVisible();
});

test("paragraph splitting and cross-section select-all edits stay attached to semantic units", async ({ page }) => {
  await page.goto("/tests/live-notes-harness.html");
  await page.getByRole("button", { name: "Markdown構造fixture" }).click();
  const editor = page.locator(".seam-document-editor[contenteditable=true]");
  const block = editor.locator('[data-unit-type="block"]');
  await block.locator("p").first().click();
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await page.keyboard.type("追記した段落");
  await expect(block.locator(":scope > p")).toHaveCount(2);
  await page.keyboard.press("Meta+A");
  await page.keyboard.type("全体を置き換えた議論メモ");
  await expect(editor.locator("[data-semantic-unit-id]")).toHaveCount(2);
  await expect(editor).toContainText("全体を置き換えた議論メモ");
  await expect(page.getByText("保存済み", { exact: true })).toBeVisible({ timeout: 5000 });
  await page.reload();
  await expect(page.locator(".seam-document-editor[contenteditable=true]")).toContainText("全体を置き換えた議論メモ");
});

test("suggestion review starts collapsed and shows readable before and after Markdown", async ({ page }) => {
  await page.goto("/tests/live-notes-harness.html");
  await page.getByRole("button", { name: "提案レビューfixture" }).click();
  const disclosure = page.locator(".seam-suggestion-review");
  await expect(disclosure).toBeVisible();
  await expect(disclosure.locator("summary")).toHaveText("確認が必要 1件");
  await expect(page.locator(".seam-document-editor").getByText("既存の認証基盤を利用する案", { exact: true })).toBeVisible();
  await expect(page.getByText("この範囲に新しいAI更新があるため、確認が必要です。", { exact: true })).toBeHidden();
  await expect(page.locator(".seam-document-suggestion pre").first()).toBeHidden();
  await disclosure.locator("summary").click();
  const review = page.locator(".seam-document-suggestion");
  await expect(review.getByText("現在のMarkdown")).toBeVisible();
  await expect(review.getByText("提案されたMarkdown")).toBeVisible();
  await expect(review.getByText("この範囲に新しいAI更新があるため、確認が必要です。")).toBeVisible();
  await expect(review.getByText("発話 7", { exact: false })).toBeVisible();
  await expect(review.getByText("候補を比較してから方式を決めましょう。")).toBeVisible();
  await expect(review.locator("pre")).toHaveCount(2);
  await expect(review).not.toContainText('"suggested_operation_json"');
  await expect(review).not.toContainText('"op":');
  await expect(page.locator(".seam-document-editor")).toContainText("既存の認証基盤を利用する案");
});

test("keep-current action resolves a suggestion and leaves the note readable", async ({ page }) => {
  await page.goto("/tests/live-notes-harness.html");
  await page.getByRole("button", { name: "提案レビューfixture" }).click();
  await page.locator(".seam-suggestion-review > summary").click();
  await page.getByRole("button", { name: "現在の内容を保つ" }).click();
  await expect(page.getByText("提案を見送りました。", { exact: true })).toBeVisible();
  await expect(page.locator(".seam-document-editor")).toContainText("既存の認証基盤を利用する案");
  await expect(page.locator(".seam-suggestion-review")).toHaveCount(0);
});

test("reflect action resolves a proposal without replacing the live editor", async ({ page }) => {
  await page.goto("/tests/live-notes-harness.html");
  await page.getByRole("button", { name: "提案レビューfixture" }).click();
  const editor = page.locator(".seam-document-editor[contenteditable=true]");
  await editor.fill("編集中の別のメモ");
  await page.locator(".seam-suggestion-review > summary").click();
  await page.getByRole("button", { name: "提案を反映" }).click();
  await expect(page.getByText("提案を反映しました。", { exact: true })).toBeVisible();
  await expect(editor).toContainText("編集中の別のメモ");
  await expect(page.locator(".seam-suggestion-review")).toHaveCount(0);
});

test("a stale suggestion action explains the conflict and preserves editor text", async ({ page }) => {
  await page.goto("/tests/live-notes-harness.html");
  await page.getByRole("button", { name: "提案レビューfixture" }).click();
  await page.getByRole("button", { name: "次の提案操作で409" }).click();
  const editor = page.locator(".seam-document-editor[contenteditable=true]");
  await editor.fill("手元で書いている内容");
  await page.locator(".seam-suggestion-review > summary").click();
  await page.getByRole("button", { name: "提案を反映" }).click();
  await expect(page.getByText("ノートが更新されました。提案を確認し直してから操作してください。", { exact: true })).toBeVisible();
  await expect(editor).toContainText("手元で書いている内容");
  await expect(page.getByRole("button", { name: "提案を反映" })).toBeEnabled();
});
