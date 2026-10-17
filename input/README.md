# input/ - feed an APK to the GitHub Actions workflow (optional)

Drop an `.apk` file in this folder and push. The **Process APK (remove ads)**
workflow runs automatically, removes the ads, and publishes the patched APK:

- as a **workflow artifact** (Actions -> run -> Artifacts), and
- as a **GitHub Release** with a public download link.

Example:

```bash
cp ~/Downloads/MyApp.apk input/MyApp.apk
git add input/MyApp.apk && git commit -m "Process MyApp" && git push
```

## Prefer not to commit the APK?

Use the manual trigger instead (nothing stored in the repo):

Actions -> **Process APK (remove ads)** -> *Run workflow* -> paste a direct
HTTPS URL of the APK.

## Notes

- This is a personal-use workflow. Remove the APK from the repo afterwards so
  the repository does not grow: `git rm input/MyApp.apk && git commit -m "cleanup"`.
- Only APKs you have the right to modify. Do not redistribute paid or licensed apps.
- Committing large files (>50 MB) is discouraged; use the URL trigger instead.
