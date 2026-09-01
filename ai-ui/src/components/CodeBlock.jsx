// SPDX-License-Identifier: Apache-2.0
export default function CodeBlock({ code, language }) {

  const [html, setHtml] = useState("");

  useEffect(() => {

    async function run() {

      const highlighter = await getShikiHighlighter();

      const highlighted = highlighter.codeToHtml(code, {
        lang: language || "text",
        theme: "one-light"
      });

      setHtml(highlighted);
    }

    run();

  }, [code, language]);

  return (

    <div
      className="
        my-4
        rounded-xl
        border border-gray-200
        overflow-hidden
        bg-white

        [&>pre]:!bg-white
        [&>pre]:!m-0
        [&>pre]:!p-4
        [&>pre]:text-[13px]
        [&>pre]:leading-6
        [&>pre]:overflow-x-auto
      "
      dangerouslySetInnerHTML={{ __html: html }}
    />

  );
}