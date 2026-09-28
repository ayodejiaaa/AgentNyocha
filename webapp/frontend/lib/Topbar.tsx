"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getFullName, logout } from "./api";

export default function Topbar({ crumb }: { crumb?: string }) {
  // localStorage doesn't exist during server rendering, so this can't be
  // read directly in the render body - the server-rendered "" and the
  // client's real value would disagree and break hydration for the whole
  // page (found the hard way: it didn't just mis-show the name, it took
  // the review page's images down with it). Reading it in an effect keeps
  // the first client render identical to the server's, then fills in the
  // real value right after mount.
  const [fullName, setFullName] = useState("");
  useEffect(() => setFullName(getFullName()), []);

  return (
    <div className="topbar">
      <Link href="/jobs" className="brand">Akowe Extraction Review</Link>
      {crumb && <span className="who">/ {crumb}</span>}
      <span className="who">{fullName}</span>
      <button className="link" onClick={logout}>Log out</button>
    </div>
  );
}
