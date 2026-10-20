import Link from "next/link";

// 상단 로고 바. right에 화면별 버튼을 넣는다.
export default function Nav({ right }: { right?: React.ReactNode }) {
  return (
    <nav className="nav">
      <Link href="/" className="logo">Day <span>Zer0</span></Link>
      <div className="row">{right}</div>
    </nav>
  );
}
