**What this changes, and why.**

**How you know it works.** On which television, or on which hardware?

---

- [ ] `cd api && python -m pytest -q` passes
- [ ] `cd api && python -m pytest -q -m integration` passes — required if you
      touched the CEC transport or the screen
- [ ] A test would have caught the bug this fixes, and I checked it by breaking
      the code on purpose
- [ ] Everything is in English: code, comments, commit messages
- [ ] Nothing hardware-specific is hard-coded
- [ ] For code: I have read [the CLA](../CLA.md) and will accept it in a comment
      (not needed for docs, profiles or tests)
