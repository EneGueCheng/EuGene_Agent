name: handwritten-tex-converter description: "触发条件：当用户上传手写学术作业的图片/照片，并要求将其转换为 LaTeX (.tex) 文档时。"
手写作业转 LaTeX 转换器 (Handwritten Homework to LaTeX Converter)
此技能将手写学术作业（以图像形式提供）直接转换为排版良好的 LaTeX 文档（或追加到已有文档中），必须严格保留用户原始解答过程。

工作流 (Workflow)
1. 识别与绝对忠实地转录
分析上传的图像以识别其中的文本和数学公式。为了保证转录结果与原图一致，请遵守以下严格规则：

划掉的文本：完全忽略用户划去或涂抹掉的任何内容。请勿将其包含在转录结果中。
模糊/不清晰的文字：利用周围的数学和逻辑上下文来推断并补全模糊或难以辨认的部分。
绝对忠实（禁止自动纠错）：如果您在用户的解答中发现计算错误、跳步、不严谨的步骤或不准确的术语，切勿在转录过程中修改或纠正它们。您必须在 LaTeX 输出中原封不动地保留用户真实的解题过程、跳过的步骤和错误，不能用您自己的推导或所谓的“正确”公式去替换。禁止捏造或想象用户手写图像中没有明确出现的内容。
2. LaTeX 排版优化
将识别出的内容转换为符合标准且语法正确的 LaTeX 代码。
在不改变用户答案底层逻辑的前提下，优化代码格式与排版（例如，使用 align* 对齐方程式，确保适当的上下间距和正确的数学环境），使其看起来整洁且专业。
题号格式：在每道独立的题号前加入加粗样式和垂直间距，使大题与大题之间有明显的段间距，题号更清晰易读。具体格式为：\vspace{2ex}\textbf{题号}，例如 \vspace{2ex}\textbf{1.} 或 \vspace{2ex}\textbf{(1)}。注意第一道题前不需要加 \vspace{2ex}。
3. 输出结构 (Output Structure)
不要仅仅在聊天框中输出代码块，请直接创建或修改工作区中的 .tex 文件。 如果一次输出内容过多，可以先修改一部分内容，然后让用户输入“继续”，从而进入下一轮生成。 当用户提供**“时间”（如：2026.4.10）和“科目”**（如：微积分）时：

如果用户要求新建文件： 请使用以下固定模板生成整个 .tex 文档：
latex
复制
\documentclass{article}
\usepackage[UTF8]{ctex}
\usepackage{amsmath}
\usepackage{amsfonts}
\usepackage{amssymb}
\usepackage{geometry}
\geometry{a4paper, margin=1in}

\begin{document}

\title{{科目}习题解答}
\author{}
\date{{时间}}
\maketitle

\section*{{时间} {科目}}

% 在此处填入转录并排版好的具体解答内容

\end{document}