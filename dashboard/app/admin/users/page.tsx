import SectionShell from "../../components/section-shell";
import UserManagement from "./user-management";

export default function AdminUsersPage() {
  return (
    <SectionShell
      active="/admin/users/"
      eyebrow="MILESPAPA QUANT LAB · ACCESS CONTROL"
      title="帳號與權限"
    >
      <UserManagement />
    </SectionShell>
  );
}
