
#include "AGVPlacementPreview.h"

#include "AGVGrabbableObject.h"
#include "Components/StaticMeshComponent.h"
#include "Materials/MaterialInterface.h"

AAGVPlacementPreview::AAGVPlacementPreview()
{
    PrimaryActorTick.bCanEverTick = false;

    PreviewMesh = CreateDefaultSubobject<UStaticMeshComponent>(
        TEXT("PreviewMesh")
    );

    RootComponent = PreviewMesh;

    // 미리보기는 충돌과 물리를 사용하지 않음
    PreviewMesh->SetCollisionEnabled(
        ECollisionEnabled::NoCollision
    );

    PreviewMesh->SetSimulatePhysics(false);
    PreviewMesh->SetEnableGravity(false);
    PreviewMesh->SetGenerateOverlapEvents(false);

    SetActorHiddenInGame(true);
}

void AAGVPlacementPreview::InitializeFromProduct(
    AAGVGrabbableObject* Product
)
{
    if (!IsValid(Product))
    {
        return;
    }

    UStaticMeshComponent* ProductMesh =
        Product->FindComponentByClass<UStaticMeshComponent>();

    if (!ProductMesh)
    {
        return;
    }

    PreviewMesh->SetStaticMesh(
        ProductMesh->GetStaticMesh()
    );

    // 실제 상품과 동일한 크기
    PreviewMesh->SetRelativeScale3D(
        ProductMesh->GetRelativeScale3D()
    );

    // 미리보기는 충돌하지 않음
    PreviewMesh->SetCollisionEnabled(
        ECollisionEnabled::NoCollision
    );

    SetActorHiddenInGame(true);
}

void AAGVPlacementPreview::UpdatePreview(
    const FTransform& SlotTransform,
    bool bCanPlace
)
{
    SetActorTransform(SlotTransform);

    // 미리보기 전체 크기를 70%로 축소
    SetActorScale3D(FVector(0.70f));

    UMaterialInterface* Material =
        bCanPlace ? ValidMaterial : InvalidMaterial;

    if (Material)
    {
        PreviewMesh->SetMaterial(0, Material);
    }

    SetActorHiddenInGame(false);
}

void AAGVPlacementPreview::HidePreview()
{
    SetActorHiddenInGame(true);
}
