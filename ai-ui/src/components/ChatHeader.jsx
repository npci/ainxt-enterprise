// SPDX-License-Identifier: Apache-2.0
import {
  Copy,
  Share,
  Pencil
} from "lucide-react";

export default function Header({ chat }) {

  return (

    <div className="h-14 border-b border-gray-200 text-md flex items-center justify-between px-4 bg-gray-50">

      <div className="flex items-center gap-2">

        <div className="text-sm font-bold text-gray-500">



        </div>

      </div>

      <div className="flex gap-3 text-gray-500">

        <Copy size={16}/>
        <Share size={16}/>
        <Pencil size={16}/>

      </div>

    </div>

  );

}