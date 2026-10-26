{pkgs, lib, imageName ? "gaoji-sandbox", imageTag ? "latest"}:
let
  sandboxToolchainVersion = "toolchain-v3-gaoji";
  python = pkgs.python312.withPackages (ps:
    with ps; [
      aiohttp
      beautifulsoup4
      httpx
      jinja2
      lxml
      odfpy
      openpyxl
      pillow
      pymupdf
      pypdf
      pytest
      pip
      python-docx
      python-pptx
      pyyaml
      reportlab
      requests
      setuptools
      wheel
      xlrd
    ]);

  # Keep the pinned package catalog in the image without merging the nixpkgs
  # source tree into /. Package preparation can therefore evaluate offline and
  # only needs the network to fetch missing binary closures.
  nixpkgsPin = pkgs.runCommand "gaoji-nixpkgs-pin" {} ''
    mkdir -p "$out/share"
    ln -s ${pkgs.path} "$out/share/nixpkgs"
  '';

  nixConfigPackage = pkgs.writeTextDir "etc/nix/nix.conf" ''
    sandbox = false
    build-users-group =
    experimental-features = nix-command
    warn-dirty = false
    keep-outputs = true
    keep-derivations = true
  '';

  cacheSeed = pkgs.writeShellScriptBin "gaoji-cache-seed" ''
    set -euo pipefail
    ${pkgs.nix}/bin/nix --extra-experimental-features read-only-local-store \
      copy --from 'local?read-only=true' \
      --to 'local?root=/cache' --all --no-check-sigs
    ${pkgs.coreutils}/bin/mkdir -p /cache/nix/var/nix/gcroots
    ${pkgs.coreutils}/bin/cp -a /nix/var/nix/gcroots/. /cache/nix/var/nix/gcroots/
    ${pkgs.coreutils}/bin/touch /cache/nix/.gaoji-cache-ready
  '';

  sandboxFontConfig = pkgs.makeFontsConf {
    fontDirectories = [
      pkgs.wqy_microhei
    ];
  };

  sandboxFontConfigPackage = pkgs.runCommand "gaoji-fontconfig" {} ''
    mkdir -p $out/etc/gaoji
    cp ${sandboxFontConfig} $out/etc/gaoji/fonts.conf
  '';

  cjkPdfTool = pkgs.writeTextFile {
    name = "gaoji-pdf";
    destination = "/bin/gaoji-pdf";
    executable = true;
    text = ''
      #!${python}/bin/python
      from __future__ import annotations

      import argparse
      import html
      import re
      from pathlib import Path

      from pypdf import PdfReader
      from reportlab.lib import colors
      from reportlab.lib.enums import TA_CENTER
      from reportlab.lib.pagesizes import A4
      from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
      from reportlab.lib.units import mm
      from reportlab.pdfbase import pdfmetrics
      from reportlab.pdfbase.ttfonts import TTFont
      from reportlab.platypus import (
          KeepTogether,
          Paragraph,
          SimpleDocTemplate,
          Spacer,
          Table,
          TableStyle,
      )

      REGULAR_FONT = "${pkgs.wqy_microhei}/share/fonts/truetype/wqy-microhei.ttc"
      FONT_NAME = "gaojiCJK"
      BOLD_NAME = FONT_NAME


      def inline_markup(value: str) -> str:
          escaped = html.escape(value.strip())
          escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
          escaped = re.sub(r"`([^`]+)`", r"<font name='gaojiCJK'>\1</font>", escaped)
          return escaped


      def markdown_story(text: str, styles: dict[str, ParagraphStyle]):
          lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
          story = []
          paragraph: list[str] = []
          bullets: list[str] = []
          code: list[str] = []
          in_code = False

          def flush_paragraph() -> None:
              if paragraph:
                  joined = " ".join(item.strip() for item in paragraph if item.strip())
                  if joined:
                      story.append(Paragraph(inline_markup(joined), styles["BodyCJK"]))
                      story.append(Spacer(1, 2.4 * mm))
                  paragraph.clear()

          def flush_bullets() -> None:
              if bullets:
                  items = [
                      Paragraph("• " + inline_markup(item), styles["BulletCJK"])
                      for item in bullets
                  ]
                  story.append(KeepTogether(items))
                  story.append(Spacer(1, 2.4 * mm))
                  bullets.clear()

          def flush_code() -> None:
              if code:
                  rendered = "<br/>".join(html.escape(item).replace(" ", "&nbsp;") for item in code)
                  story.append(Paragraph(rendered or " ", styles["CodeCJK"]))
                  story.append(Spacer(1, 2.8 * mm))
                  code.clear()

          index = 0
          while index < len(lines):
              line = lines[index]
              stripped = line.strip()
              if stripped.startswith("```"):
                  flush_paragraph()
                  flush_bullets()
                  if in_code:
                      flush_code()
                  in_code = not in_code
                  index += 1
                  continue
              if in_code:
                  code.append(line)
                  index += 1
                  continue
              if not stripped:
                  flush_paragraph()
                  flush_bullets()
                  index += 1
                  continue
              heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
              if heading:
                  flush_paragraph()
                  flush_bullets()
                  level = len(heading.group(1))
                  story.append(Paragraph(inline_markup(heading.group(2)), styles[f"H{level}CJK"]))
                  story.append(Spacer(1, (4 - level) * mm))
                  index += 1
                  continue
              if re.match(r"^[-*+]\s+", stripped):
                  flush_paragraph()
                  bullets.append(re.sub(r"^[-*+]\s+", "", stripped))
                  index += 1
                  continue
              if "|" in stripped and index + 1 < len(lines) and re.match(
                  r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$",
                  lines[index + 1],
              ):
                  flush_paragraph()
                  flush_bullets()
                  rows: list[list[str]] = []
                  header = [part.strip() for part in stripped.strip("|").split("|")]
                  rows.append(header)
                  index += 2
                  while index < len(lines) and "|" in lines[index] and lines[index].strip():
                      rows.append([part.strip() for part in lines[index].strip().strip("|").split("|")])
                      index += 1
                  width = max(len(row) for row in rows)
                  normalized = [row + [""] * (width - len(row)) for row in rows]
                  cells = [
                      [Paragraph(inline_markup(cell), styles["TableCJK"]) for cell in row]
                      for row in normalized
                  ]
                  table = Table(cells, repeatRows=1, hAlign="LEFT")
                  table.setStyle(TableStyle([
                      ("FONTNAME", (0, 0), (-1, 0), BOLD_NAME),
                      ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF2")),
                      ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#AAB7C0")),
                      ("VALIGN", (0, 0), (-1, -1), "TOP"),
                      ("LEFTPADDING", (0, 0), (-1, -1), 6),
                      ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                      ("TOPPADDING", (0, 0), (-1, -1), 5),
                      ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                  ]))
                  story.append(table)
                  story.append(Spacer(1, 3 * mm))
                  continue
              paragraph.append(line)
              index += 1

          flush_paragraph()
          flush_bullets()
          flush_code()
          return story


      def main() -> int:
          parser = argparse.ArgumentParser(description="Create a CJK-safe PDF from UTF-8 Markdown.")
          parser.add_argument("input", help="UTF-8 Markdown input")
          parser.add_argument("output", help="PDF output path")
          parser.add_argument("--title", default="", help="PDF metadata title")
          args = parser.parse_args()

          source = Path(args.input)
          output = Path(args.output)
          text = source.read_text(encoding="utf-8")
          output.parent.mkdir(parents=True, exist_ok=True)

          pdfmetrics.registerFont(TTFont(FONT_NAME, REGULAR_FONT))
          pdfmetrics.registerFontFamily(
              FONT_NAME,
              normal=FONT_NAME,
              bold=BOLD_NAME,
              italic=FONT_NAME,
              boldItalic=BOLD_NAME,
          )

          base = getSampleStyleSheet()
          styles = {
              "BodyCJK": ParagraphStyle(
                  "BodyCJK", parent=base["BodyText"], fontName=FONT_NAME,
                  fontSize=10.5, leading=17, wordWrap="CJK", textColor=colors.HexColor("#182026"),
              ),
              "BulletCJK": ParagraphStyle(
                  "BulletCJK", parent=base["BodyText"], fontName=FONT_NAME,
                  fontSize=10.5, leading=17, leftIndent=5 * mm, firstLineIndent=-4 * mm, wordWrap="CJK",
              ),
              "H1CJK": ParagraphStyle(
                  "H1CJK", parent=base["Heading1"], fontName=BOLD_NAME,
                  fontSize=20, leading=27, wordWrap="CJK", textColor=colors.HexColor("#111827"),
              ),
              "H2CJK": ParagraphStyle(
                  "H2CJK", parent=base["Heading2"], fontName=BOLD_NAME,
                  fontSize=15, leading=22, wordWrap="CJK", textColor=colors.HexColor("#1F2937"),
              ),
              "H3CJK": ParagraphStyle(
                  "H3CJK", parent=base["Heading3"], fontName=BOLD_NAME,
                  fontSize=12, leading=19, wordWrap="CJK", textColor=colors.HexColor("#374151"),
              ),
              "CodeCJK": ParagraphStyle(
                  "CodeCJK", parent=base["Code"], fontName=FONT_NAME,
                  fontSize=8.5, leading=13, borderColor=colors.HexColor("#CBD5E1"),
                  borderWidth=0.5, borderPadding=8, backColor=colors.HexColor("#F8FAFC"), wordWrap="CJK",
              ),
              "TableCJK": ParagraphStyle(
                  "TableCJK", parent=base["BodyText"], fontName=FONT_NAME,
                  fontSize=8.5, leading=13, wordWrap="CJK",
              ),
          }
          title = args.title.strip() or source.stem
          document = SimpleDocTemplate(
              str(output), pagesize=A4, title=title,
              leftMargin=18 * mm, rightMargin=18 * mm,
              topMargin=18 * mm, bottomMargin=18 * mm,
          )

          def footer(canvas, doc) -> None:
              canvas.saveState()
              canvas.setFont(FONT_NAME, 8)
              canvas.setFillColor(colors.HexColor("#64748B"))
              canvas.drawCentredString(A4[0] / 2, 8 * mm, str(doc.page))
              canvas.restoreState()

          story = markdown_story(text, styles)
          if not story:
              story = [Paragraph("（空文档）", styles["BodyCJK"])]
          document.build(story, onFirstPage=footer, onLaterPages=footer)

          reader = PdfReader(str(output))
          extracted = "".join(page.extract_text() or "" for page in reader.pages).strip()
          if not reader.pages or (text.strip() and not extracted):
              output.unlink(missing_ok=True)
              raise SystemExit("PDF verification failed: no extractable text")
          print(f"created {output} ({len(reader.pages)} page(s), embedded CJK font)")
          return 0


      if __name__ == "__main__":
          raise SystemExit(main())
    '';
  };

  # The base is intentionally boring and useful. Large specialist stacks are
  # supplied per command through sandbox_exec.packages and cached in /nix.
  tools = with pkgs; [
    nix
    bashInteractive
    coreutils
    gnused
    gawk
    gnugrep
    findutils
    diffutils
    patch
    file
    tree
    bc
    less
    which
    procps
    psmisc
    lsof
    util-linux
    hostname
    gnutar
    gzip
    xz
    bzip2
    zstd
    zip
    unzip
    p7zip
    curl
    wget
    openssl
    rsync
    socat
    netcat-gnu
    iproute2
    iputils
    dnsutils
    openssh
    cacert
    git
    vim
    nano
    jq
    ripgrep
    gnumake
    gcc
    cmake
    ninja
    pkg-config
    shellcheck
    python
    nodejs_22
    sqlite
    poppler-utils
    qpdf
    fontconfig
    wqy_microhei
    cjkPdfTool
    cacheSeed
  ];

  baseEnvironment = pkgs.buildEnv {
    name = "gaoji-sandbox-base";
    paths = tools;
    pathsToLink = ["/bin" "/share"];
  };
in
pkgs.dockerTools.buildLayeredImageWithNixDb {
  name = imageName;
  tag = imageTag;
  maxLayers = 120;
  contents = [
    baseEnvironment
    sandboxFontConfigPackage
    nixConfigPackage
    nixpkgsPin
    pkgs.dockerTools.binSh
    pkgs.dockerTools.usrBinEnv
  ];

  extraCommands = ''
    mkdir -p workspace home/sandbox tmp etc nix/var/nix/gcroots/gaoji-packages
    # Nix builders cannot materialize arbitrary numeric ownership in every
    # sandbox backend. The container is isolated and runs as uid 1000, so make
    # its private workspace and home writable without a build-time chown.
    chmod 0777 workspace home/sandbox
    chmod 1777 tmp
    printf 'sandbox:x:1000:1000:gaoji sandbox:/home/sandbox:/bin/sh\n' > etc/passwd
    printf 'sandbox:x:1000:\n' > etc/group
    printf 'hosts: files dns\n' > etc/nsswitch.conf
    ln -s ${baseEnvironment} nix/var/nix/gcroots/gaoji-base
    printf '%s\n' \
      'Languages: Python 3.12 with pip, Node.js 22 with npm' \
      'Development: git, gcc, make, pkg-config, shellcheck' \
      'Documents: poppler, qpdf, Python PDF and Office libraries' \
      'CJK PDF: gaoji-pdf input.md output.pdf (embedded Chinese font)' \
      'Data: SQLite, JSON/YAML/XML parsers' \
      'On demand: pass nixpkgs attributes in sandbox_exec.packages' \
      > etc/gaoji-sandbox-tools
  '';

  config = {
    User = "1000:1000";
    WorkingDir = "/workspace";
    Cmd = ["${pkgs.coreutils}/bin/sleep" "infinity"];
    Env = [
      "PATH=${lib.makeBinPath tools}:/bin:/usr/bin"
      "HOME=/home/sandbox"
      "USER=sandbox"
      "LANG=C.UTF-8"
      "LC_ALL=C.UTF-8"
      "NIX_PATH=nixpkgs=${nixpkgsPin}/share/nixpkgs"
      "NIX_PAGER=cat"
      "FONTCONFIG_FILE=/etc/gaoji/fonts.conf"
      "PDF_CJK_FONT=${pkgs.wqy_microhei}/share/fonts/truetype/wqy-microhei.ttc"
      "PDF_CJK_BOLD_FONT=${pkgs.wqy_microhei}/share/fonts/truetype/wqy-microhei.ttc"
      "PYTHONUNBUFFERED=1"
      "MPLCONFIGDIR=/tmp/matplotlib"
      "XDG_CACHE_HOME=/tmp/cache"
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
    ];
    Labels = {
      "org.opencontainers.image.title" = "gaoji advanced sandbox";
      # Keep this independent from the Bot release. Bump it only when the
      # sandbox toolchain itself has a compatibility-breaking change.
      "org.opencontainers.image.version" = sandboxToolchainVersion;
      "io.gaoji.sandbox" = "advanced";
    } // lib.optionalAttrs (imageName == "gaoji-app-sandbox") {
      "io.gaoji.app.recipe" = imageTag;
    };
  };
}
