import Link from "next/link";

export default function Home() {
  return (
    <main className="page">
      <h1>Day Zero</h1>
      <p className="muted">입사 첫날 전에, AI 동료와 함께 일해 보는 하루. (MVP)</p>
      <div className="card">
        <h2>기업 채용 담당자</h2>
        <p>로그인해서 지원자를 초대하고 리포트를 확인합니다.</p>
        <Link href="/admin/login">관리자 로그인 →</Link>
      </div>
      <div className="card">
        <h2>지원자</h2>
        <p className="muted">채용 담당자에게 받은 초대 링크로 입장하세요.</p>
      </div>
    </main>
  );
}
