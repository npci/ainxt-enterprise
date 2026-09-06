// SPDX-License-Identifier: MIT
import { useEffect, useState } from "react";
import { API_BASE, authFetch } from '../config';

export default function TracePanel() {

  const [logs, setLogs] = useState([]);

  async function fetchLogs() {

    try {

      const res = await authFetch(
        `${API_BASE}/audit`
      );

      const data = await res.json();

      setLogs(data.logs || []);

    } catch (e) {

      console.error(e);

    }
  }

  useEffect(() => {

    fetchLogs();

    const interval = setInterval(fetchLogs, 2000);

    return () => clearInterval(interval);

  }, []);

  return (

    <div className="h-full overflow-auto p-4 text-xs font-mono">

      <div className="font-bold mb-2">
        Audit Log
      </div>

      {logs.map((line, i) => (

        <div key={i}>
          {line}
        </div>

      ))}

    </div>
  );
}